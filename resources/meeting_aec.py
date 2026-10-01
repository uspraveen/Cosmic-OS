#!/usr/bin/env python3
"""Echo cancellation for Cosmic meeting mode.

Origin: ported from the cosmicV2 meeting engine (aec_py.py), a block NLMS
echo canceller at 16 kHz. During the port, three latent bugs in the original
were found and fixed (the original could never actually cancel anything):

1. The double-talk detector compared near energy against the echo ESTIMATE
   with a 1e-6 silence guard, but the shared _rms() helper floors at 1e-6 -
   so the guard never fired, DTD triggered on the very first block (echo
   estimate still zero), and adaptation froze at its first tiny step.
   Replaced with a correlation-based DTD: freeze when the near signal stops
   correlating with the far reference. Bootstrap-safe (pure echo correlates
   with far regardless of gain or convergence state) and gain-independent.

2. The echo-estimate and gradient slices of the FFT convolution were
   mis-indexed (off by L-1 samples). Moot after 3:

3. A block-gradient NLMS update is structurally unstable here: 2048 taps
   estimated from 160 samples per block is a massively rank-deficient
   descent direction - it converges for a few iterations and then diverges
   (verified empirically during this port). The core is now the textbook
   stable form: sequential per-sample NLMS.

Reference model (end-anchored, in MicEchoCanceller): the canceller keeps a
bounded backlog of far-end audio. The echo of the current mic frame sits D
samples before the newest far-end sample, where D is the unknown acoustic +
buffering path delay. The canceller cross-correlates the mic frame against a
search window around the current D estimate and slides D to the correlation
peak. When near-end speech dominates or the peak is weak, D is frozen - we
never "correct" on a guess.

Pass-through guarantees: without numpy, without far-end audio, or while
unaligned, process() returns the mic frame unchanged. The canceller can only
remove audio, never corrupt the mic leg.

This is intentionally production-lite: it is not WebRTC-grade AEC. Its job is
to keep leaked far-end speech out of the "Me" transcript, not studio quality.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is expected but optional
    np = None
    HAS_NUMPY = False


TARGET_SR = 16000
MIC_FRAME_BYTES = 3200  # 100 ms of 16 kHz mono int16


def _preemph(x, a=0.7):
    if a <= 0:
        return x
    y = x.copy()
    y[1:] -= a * x[:-1]
    return y


def _rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float32) ** 2) + 1e-12))


@dataclass
class AECConfig:
    sr: int = TARGET_SR
    frame_ms: int = 10
    tail_ms: int = 128
    mu: float = 0.3             # per-sample NLMS step (stable for 0 < mu < 2)
    leak: float = 1e-6
    eps: float = 1e-8
    preemph: float = 0.7
    dtd_corr_min: float = 0.35  # freeze adaptation when near stops looking like far
    dtd_hold: int = 4
    ref_gain_db: float = 0.0
    fixed_delay_ms: int = 0     # the aligner owns delay discovery; keep the filter compact


class NLMS_AEC:
    """Sequential per-sample NLMS echo canceller (10 ms blocks)."""

    def __init__(self, cfg: AECConfig):
        self.cfg = cfg
        self.N = int(cfg.sr * cfg.frame_ms / 1000)
        self.L = int(cfg.sr * cfg.tail_ms / 1000)
        self.delay = int(cfg.sr * cfg.fixed_delay_ms / 1000)

        self.w = np.zeros(self.L, dtype=np.float32)
        self.ref_hist = np.zeros(self.L - 1 + self.N + self.delay, dtype=np.float32)
        self.dtd_count = 0
        self._rg = 10.0 ** (cfg.ref_gain_db / 20.0) if abs(cfg.ref_gain_db) > 1e-3 else 1.0
        self._deemph_state = 0.0

    def _double_talk(self, near_p, x_block) -> bool:
        nx = float(np.dot(near_p, x_block))
        nn = float(np.dot(near_p, near_p))
        xx = float(np.dot(x_block, x_block))
        if nn < 1e-4 or xx < 1e-4:
            return False  # nothing to correlate; adaptation would be a no-op
        corr = abs(nx) / ((nn * xx) ** 0.5)
        return corr < self.cfg.dtd_corr_min

    def process_block(self, near_block, ref_block):
        # Work in the pre-emphasized domain on BOTH sides: pre-emphasis
        # commutes with the (linear) echo path, so the filter still fits a
        # short tap profile, and the residual is de-emphasized back to the raw
        # near domain at the end.
        if self.cfg.preemph > 0:
            n_p = _preemph(near_block.astype(np.float32), self.cfg.preemph)
            r_p = _preemph(ref_block.astype(np.float32) * self._rg, self.cfg.preemph)
        else:
            n_p = near_block.astype(np.float32)
            r_p = ref_block.astype(np.float32) * self._rg

        if self.delay > 0:
            self.ref_hist = np.concatenate([self.ref_hist, np.zeros(self.delay, dtype=np.float32)])
        self.ref_hist = np.concatenate([self.ref_hist, r_p])

        start = len(self.ref_hist) - (self.L - 1 + self.N)
        xseg = self.ref_hist[start:]

        # Per-block double-talk gate (correlation between near and far).
        x_block = xseg[self.L - 1: self.L - 1 + self.N]
        dt = self._double_talk(n_p, x_block)
        if dt:
            self.dtd_count = max(self.dtd_count, self.cfg.dtd_hold)
        adapt = (self.dtd_count == 0)

        mu = self.cfg.mu
        eps = self.cfg.eps
        leak = 1.0 - self.cfg.leak
        w = self.w
        e_p = np.empty(self.N, dtype=np.float32)

        # Sequential per-sample NLMS. y[t] = sum_k w[k] * xseg[L-1+t-k], i.e.
        # dot(w, v_t) with v_t = xseg[t : t+L][::-1] (the L-sample tap window
        # ending at the current input sample). Updating w inside the loop is
        # what makes this the stable form.
        for t in range(self.N):
            v = xseg[t: t + self.L][::-1]
            y_t = float(np.dot(w, v))
            e_t = n_p[t] - y_t
            e_p[t] = e_t
            if adapt:
                p = float(np.dot(v, v))
                if p > eps:
                    w += (mu * e_t / (p + eps)) * v
                    if leak < 1.0:
                        w *= leak

        self.ref_hist = self.ref_hist[-(self.L - 1 + self.N):]

        # De-emphasize the residual back to the raw near domain (exact inverse
        # of the pre-emphasis differentiator, stable one-pole integrator).
        out = np.empty(self.N, dtype=np.float64)
        acc = self._deemph_state
        a = self.cfg.preemph
        for i in range(self.N):
            acc = float(e_p[i]) + a * acc
            out[i] = acc
        self._deemph_state = acc
        return np.clip(out, -32768.0, 32767.0).astype(np.int16)


class MicEchoCanceller:
    """Mic-leg wrapper: end-anchored far-end reference + NLMS, pass-through safe."""

    SEARCH_SAMPLES = 480          # aligned-phase drift window (~30 ms)
    RESYNC_EVERY_FRAMES = 50      # aligned-phase drift check cadence (~5 s)
    RESIDUAL_HIGH_RUN = 8         # frames of exploded residual before forced resync
    ALIGN_CORRELATION_MIN = 0.55  # normalized peak required to trust a resync
    MIN_REF_SAMPLES = 320         # at least 20 ms of far-end before trying
    KEEP_MARGIN_SAMPLES = 960     # extra backlog kept beyond (D + frame)
    UNALIGNED_KEEP_SAMPLES = 4800  # 300 ms backlog while hunting for the path
    COARSE_STEP = 16              # coarse search step while unaligned
    COARSE_DOWNSAMPLE = 4         # decimation for the coarse pass

    def __init__(self, cfg: AECConfig | None = None, frame_bytes: int = MIC_FRAME_BYTES):
        self.enabled = HAS_NUMPY
        self.frame_bytes = frame_bytes
        self.frame_samples = frame_bytes // 2
        self.delay_samples = 400   # initial path-delay guess; alignment refines it fast
        if not self.enabled:
            return
        self.aec = NLMS_AEC(cfg or AECConfig())
        self._ref = bytearray()       # far-end 16 kHz mono int16
        self._mic_tail = bytearray()  # bytes waiting to complete a frame
        self._frames_seen = 0
        self._aligned = False
        self._res_baseline_rms = 120.0
        self._res_high_run = 0

    # ---- far-end feeding -------------------------------------------------

    def push_far(self, data_16k_mono: bytes) -> None:
        """Feed converted far-end audio (any chunk size, 16 kHz mono int16)."""
        if not self.enabled or not data_16k_mono:
            return
        self._ref.extend(data_16k_mono)
        self._trim_head()

    def _keep_samples(self) -> int:
        if not self._aligned:
            return self.UNALIGNED_KEEP_SAMPLES
        return self.delay_samples + self.frame_samples + self.KEEP_MARGIN_SAMPLES

    def _trim_head(self) -> None:
        excess = len(self._ref) // 2 - self._keep_samples()
        if excess > 0:
            del self._ref[:excess * 2]

    # ---- mic processing ---------------------------------------------------

    def process(self, mic_frame: bytes) -> bytes:
        """Echo-reduce one mic frame. Always returns 16 kHz mono int16."""
        if not self.enabled or not mic_frame:
            return mic_frame

        self._mic_tail.extend(mic_frame)
        if len(self._mic_tail) < self.frame_bytes:
            return b""

        frame = bytes(self._mic_tail[:self.frame_bytes])
        del self._mic_tail[:self.frame_bytes]
        self._frames_seen += 1

        ref_samples = len(self._ref) // 2
        if ref_samples < max(self.MIN_REF_SAMPLES, self.frame_samples):
            # Far-end silent or not yet flowing: nothing to cancel.
            return frame

        if not self._aligned or self._frames_seen % self.RESYNC_EVERY_FRAMES == 0:
            self._try_resync(frame)

        if not self._aligned:
            return frame

        end = ref_samples - self.delay_samples
        begin = end - self.frame_samples
        if begin < 0:
            return frame
        ref = np.frombuffer(
            bytes(self._ref[begin * 2:end * 2]), dtype=np.int16
        ).astype(np.int16)

        near = np.frombuffer(frame, dtype=np.int16)
        out = np.empty(self.frame_samples, dtype=np.int16)
        sub = self.aec.N
        for i in range(0, self.frame_samples, sub):
            chunk = min(sub, self.frame_samples - i)
            out[i:i + chunk] = self.aec.process_block(
                near[i:i + chunk], ref[i:i + chunk],
            )[:chunk]

        self._track_residual(frame, out)
        self._trim_head()
        return out.tobytes()

    def _track_residual(self, frame: bytes, out) -> None:
        """Force a resync when cancellation suddenly stops working (path
        changed, device switched). The forced resync is self-guarding: during
        genuine near speech the correlation peak is weak, so no change is made
        and only the CPU cost of the search is paid."""
        out_rms = _rms(out.astype(np.float32))
        in_rms = _rms(np.frombuffer(frame, dtype=np.int16).astype(np.float32))
        if in_rms < 24.0:
            return
        if out_rms > max(4.0 * self._res_baseline_rms, 150.0):
            self._res_high_run += 1
            if self._res_high_run >= self.RESIDUAL_HIGH_RUN:
                self._res_high_run = 0
                self._try_resync(frame)
                if self._aligned:
                    self._res_baseline_rms = max(30.0, out_rms)
        else:
            self._res_high_run = 0
            if out_rms > 1.0:
                self._res_baseline_rms = (self._res_baseline_rms * 0.9) + (out_rms * 0.1)

    # ---- alignment ---------------------------------------------------------

    def _try_resync(self, frame: bytes) -> None:
        """Slide the delay estimate to the correlation peak, or freeze it."""
        near = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        near_energy = _rms(near)
        if near_energy < 24.0:
            return  # nothing meaningful in the mic frame; keep the current delay

        ref_samples = len(self._ref) // 2
        if ref_samples < max(self.MIN_REF_SAMPLES, self.frame_samples):
            return

        if self._aligned:
            search_lo = max(0, self.delay_samples - self.SEARCH_SAMPLES)
            search_hi = min(
                ref_samples - self.frame_samples,
                self.delay_samples + self.SEARCH_SAMPLES,
            )
            if search_hi <= search_lo:
                return
            candidates = range(search_lo, search_hi + 1, 8)
        else:
            # Initial alignment: coarse sweep over the entire backlog.
            candidates = range(0, ref_samples - self.frame_samples + 1, self.COARSE_STEP)
            if not candidates:
                return

        ref = np.frombuffer(bytes(self._ref), dtype=np.int16).astype(np.float32)
        near_sq = float(np.dot(near, near))
        if near_sq <= 0:
            return

        if not self._aligned:
            near_c = near[:: self.COARSE_DOWNSAMPLE]
            near_sq_c = float(np.dot(near_c, near_c))
            if near_sq_c <= 0:
                return

        best_delay = None
        best_score = 0.0
        for delay in candidates:
            begin = ref_samples - delay - self.frame_samples
            end = ref_samples - delay
            seg = ref[begin:end]
            if not self._aligned:
                seg = seg[:: self.COARSE_DOWNSAMPLE]
                seg_sq = float(np.dot(seg, seg))
                if seg_sq <= 0:
                    continue
                score = abs(float(np.dot(near_c, seg)) / ((near_sq_c * seg_sq) ** 0.5))
            else:
                seg_sq = float(np.dot(seg, seg))
                if seg_sq <= 0:
                    continue
                score = abs(float(np.dot(near, seg)) / ((near_sq * seg_sq) ** 0.5))
            if score > best_score:
                best_score = score
                best_delay = delay

        if best_delay is None or best_score < self.ALIGN_CORRELATION_MIN:
            return

        if not self._aligned and self.COARSE_DOWNSAMPLE > 1:
            # Refine the coarse peak at full resolution before locking on.
            refine_lo = max(0, best_delay - self.COARSE_STEP)
            refine_hi = min(ref_samples - self.frame_samples, best_delay + self.COARSE_STEP)
            best_full = best_delay
            best_full_score = best_score
            for delay in range(refine_lo, refine_hi + 1, 2):
                begin = ref_samples - delay - self.frame_samples
                end = ref_samples - delay
                seg = ref[begin:end]
                seg_sq = float(np.dot(seg, seg))
                if seg_sq <= 0:
                    continue
                score = abs(float(np.dot(near, seg)) / ((near_sq * seg_sq) ** 0.5))
                if score > best_full_score:
                    best_full_score = score
                    best_full = delay
            if best_full_score >= self.ALIGN_CORRELATION_MIN:
                best_delay = best_full
                best_score = best_full_score

        self.delay_samples = best_delay
        self._aligned = True
        self._trim_head()
