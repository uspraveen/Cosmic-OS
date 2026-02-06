import { useEffect, useRef } from 'react'

interface WeatherAnimationProps {
  condition: string
  isDay: boolean
  className?: string
  snowfall?: number
}

interface Particle {
  x: number
  y: number
  z: number
  speed: number
  opacity: number
  twinkle: number
  drift: number
  swayOffset: number
}

interface Splash {
  x: number
  y: number
  vx: number
  vy: number
  life: number
  maxLife: number
}

// New interfaces
interface Ray {
  angle: number
  length: number
  speed: number
}

interface Sun {
  x: number
  y: number
  radius: number
  bloomRadius: number
  rotation: number
}

interface Moon {
  x: number
  y: number
  radius: number
  craters: { x: number, y: number, r: number }[]
}

export default function WeatherAnimation({ condition, isDay, className, snowfall }: WeatherAnimationProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  // Normalize condition
  const c = (condition || '').toLowerCase()
  const isClear = c.includes('clear') || c.includes('mainly')
  const isRain = c.includes('rain') || c.includes('drizzle') || c.includes('shower')
  const isSnow = c.includes('snow') || c.includes('freezing')
  const isThunder = c.includes('thunder')
  const isFog = c.includes('fog') || c.includes('mist')

  const isPartly = c.includes('partly')
  const isCloudy = isPartly || c.includes('cloud') || c.includes('overcast')
  const showSubtleClouds = isClear || isCloudy

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const resizeObserver = new ResizeObserver(() => {
      const rect = canvas.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      canvas.width = rect.width * dpr
      canvas.height = rect.height * dpr
      ctx.scale(dpr, dpr)
    })
    resizeObserver.observe(canvas)

    let animationId: number
    const particles: Particle[] = []
    const splashes: Splash[] = []

    // --- CONFIGURATION ---
    const activeRain = isRain || isThunder
    const activeSnow = isSnow
    const activeStars = (isClear || isPartly) && !isDay
    const activeFog = isFog
    const activeBird = (isClear || isPartly) && isDay

    // Celestial Bodies
    const activeSun = (isClear || isPartly) && isDay
    const activeMoon = (isClear || isPartly) && !isDay

    let sun: Sun | null = null
    let moon: Moon | null = null

    // Initialize Sun
  if (activeSun) {
  sun = {
    x: 80, 
    y: 50,
    radius: 22,
    bloomRadius: 65,
    rotation: 0
  }
}
    // Initialize Moon
    if (activeMoon) {
      moon = {
        x: 80,
        y: 28, // Moved up significantly (was 50)
        radius: 22,
        craters: [
          { x: -8, y: -6, r: 6 },
          { x: 10, y: -2, r: 4 },
          { x: -5, y: 10, r: 5 },
          { x: 8, y: 8, r: 3 },
          { x: -12, y: 3, r: 2.5 }
        ]
      }
    }

    // Particle counts
    const count = activeRain ? 100 : activeSnow ? 50 : activeStars ? 40 : activeFog ? 20 : 0

    // Initialize Particles
    for (let i = 0; i < count; i++) {
      particles.push({
        x: Math.random() * 500,
        y: Math.random() * 200,
        z: Math.random() * 0.5 + 0.5,
        speed: activeRain ? (Math.random() * 8 + 15) : (Math.random() * 1 + 0.5),
        opacity: Math.random(),
        twinkle: Math.random() * 0.03,
        drift: Math.random() * 0.5 - 0.25,
        swayOffset: Math.random() * Math.PI * 2
      })
    }

    // --- CLOUD STATE ---
    const clouds: any[] = []
    if (showSubtleClouds) {
      // More clouds if it's actually cloudy/partly, fewer if just "Clear" (subtle drift)
      const cloudCount = isCloudy ? (isPartly ? 5 : 8) : 2
      for (let i = 0; i < cloudCount; i++) {
        clouds.push({
          x: Math.random() * 500,
          y: Math.random() * 100, // Clouds in upper area
          speed: 0.1 + Math.random() * 0.2,
          size: 30 + Math.random() * 30,
          opacity: isCloudy ? 0.4 : 0.15
        })
      }
    }

    // --- BIRD STATE ---
    const birds: any[] = []
    if (activeBird) {
      const birdCount = isPartly ? 3 : 1 // More birds on nice partly cloudy days?
      for (let i = 0; i < birdCount; i++) {
        birds.push({
          x: -40 - (i * 100),
          y: 40 + Math.random() * 30,
          speed: 0.6 + Math.random() * 0.2,
          flapPhase: Math.random() * Math.PI,
          flapSpeed: 0.08,
          glideTimer: 0,
          isGliding: true,
          size: 0.9 + Math.random() * 0.2
        })
      }
    }

    // --- STREET LIGHT STATE ---
    // Only active if snowing and there is accumulation (snowfall > 0)
    const activeStreetLight = activeSnow && (typeof snowfall === 'number' && snowfall > 0.5) // Threshold for "good accumulation"

    const draw = () => {
      const width = canvas.width / (window.devicePixelRatio || 1)
      const height = canvas.height / (window.devicePixelRatio || 1)

      ctx.clearRect(0, 0, width, height)

      // 0. Thunder Flash
      if (isThunder && Math.random() > 0.98) {
        ctx.fillStyle = `rgba(255, 255, 255, ${Math.random() * 0.15})`
        ctx.fillRect(0, 0, width, height)
      }

      // --- STREET LIGHT POLE (Background) ---
      if (activeStreetLight) {
        // Move pole slightly left (was width - 40)
        const poleX = width - 65
        const poleY = height
        const poleH = 135 // Taller sleek pole

        ctx.save()

        // 1. Pole Body - Sleek dark grey with highlight
        ctx.fillStyle = "#1e1e1e"
        ctx.fillRect(poleX, poleY - poleH, 5, poleH)

        // Highlight on pole (rim lighting)
        ctx.fillStyle = "#333"
        ctx.fillRect(poleX, poleY - poleH, 1, poleH)

        // 2. Pole Arm - More modern curve
        ctx.beginPath()
        ctx.moveTo(poleX + 2, poleY - poleH)
        // Curve upwards and left
        ctx.bezierCurveTo(poleX + 2, poleY - poleH - 25, poleX - 10, poleY - poleH - 25, poleX - 25, poleY - poleH - 18)
        ctx.lineWidth = 4
        ctx.strokeStyle = "#1e1e1e"
        ctx.stroke()

        // 3. Lamp Head - Angled
        const lampX = poleX - 28
        const lampY = poleY - poleH - 16

        ctx.save()
        ctx.translate(lampX, lampY)
        ctx.rotate(-Math.PI / 6) // Tilt lamp head
        ctx.fillStyle = "#2a2a2a"
        ctx.beginPath()
        ctx.ellipse(0, 0, 10, 5, 0, 0, Math.PI * 2)
        ctx.fill()
        ctx.restore()

        // 4. Slanting Warm Light Glow (Cone)
        // Simulate light casting down-left
        ctx.save()
        ctx.translate(lampX, lampY)
        ctx.rotate(Math.PI / 8) // Angle the lightcone

        const lightGrad = ctx.createRadialGradient(0, 5, 2, 0, 60, 80)
        lightGrad.addColorStop(0, "rgba(255, 220, 120, 0.95)") // Hot core
        lightGrad.addColorStop(0.3, "rgba(255, 190, 80, 0.35)") // Warm spread
        lightGrad.addColorStop(1, "rgba(255, 180, 60, 0)") // Fade

        ctx.fillStyle = lightGrad
        ctx.beginPath()
        ctx.moveTo(-8, 0)
        ctx.lineTo(8, 0)
        ctx.lineTo(50, 140) // Left side of cone
        ctx.lineTo(-30, 140) // Right side of cone
        ctx.closePath()
        ctx.fill()
        ctx.restore()

        // 5. Snow Accumulation Check & Glow
        // We can't really "stack" particles in this simple system without physics,
        // but we can make the ground glow where snow counts
        if (snowfall && snowfall > 0.5) {
          const groundGlow = ctx.createRadialGradient(lampX - 20, height, 10, lampX - 20, height, 70)
          groundGlow.addColorStop(0, "rgba(255, 200, 80, 0.4)")
          groundGlow.addColorStop(1, "rgba(255, 200, 80, 0)")

          ctx.fillStyle = groundGlow
          ctx.fillRect(lampX - 100, height - 40, 160, 40)
        }

        // 6. Connecting Wire - Shortened path to top
        ctx.beginPath()
        ctx.moveTo(poleX + 3, poleY - poleH + 10)
        // Curve to top-mid-left area instead of far left
        // Control point: makes it droop
        // End point: somewhere in the sky
        ctx.bezierCurveTo(poleX - 20, poleY - poleH + 20, width * 0.7, 40, width * 0.6, 0)
        ctx.lineWidth = 1.2
        ctx.strokeStyle = "rgba(255, 255, 255, 0.08)"
        ctx.stroke()

        ctx.restore()
      }

      // 1. Draw Celestial Body (Sun/Moon) BEFORE clouds
      if (sun) {
  sun.rotation += 0.005 // Control shimmer speed

  ctx.save()
  ctx.translate(sun.x, sun.y)

  // 1. Large Atmospheric Bloom (Softest outer layer)
  const atmosphericGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, sun.bloomRadius * 2);
  atmosphericGrad.addColorStop(0, 'rgba(255, 230, 150, 0.15)');
  atmosphericGrad.addColorStop(1, 'rgba(255, 200, 50, 0)');
  ctx.fillStyle = atmosphericGrad;
  ctx.beginPath();
  ctx.arc(0, 0, sun.bloomRadius * 2, 0, Math.PI * 2);
  ctx.fill();

  // 2. Shimmering Corona (Procedural noise-like glow)
  ctx.rotate(sun.rotation);
  for (let i = 0; i < 3; i++) {
    ctx.rotate(Math.PI / 1.5);
    const coronaGrad = ctx.createRadialGradient(0, 0, sun.radius, 0, 0, sun.bloomRadius);
    coronaGrad.addColorStop(0, 'rgba(255, 240, 180, 0.4)');
    coronaGrad.addColorStop(1, 'rgba(255, 220, 100, 0)');
    
    ctx.save();
    ctx.scale(1.2, 0.8); // Create an elliptical shimmer
    ctx.fillStyle = coronaGrad;
    ctx.beginPath();
    ctx.arc(0, 0, sun.bloomRadius, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // 3. Core Sun Disk
  const coreGrad = ctx.createRadialGradient(-sun.radius * 0.3, -sun.radius * 0.3, 2, 0, 0, sun.radius);
  coreGrad.addColorStop(0, '#FFFFFF'); // Specular highlight
  coreGrad.addColorStop(0.2, '#FFF5CC');
  coreGrad.addColorStop(1, '#FFD700');
  
  ctx.shadowColor = 'rgba(255, 215, 0, 0.8)';
  ctx.shadowBlur = 25;
  ctx.fillStyle = coreGrad;
  ctx.beginPath();
  ctx.arc(0, 0, sun.radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;

  ctx.restore();
}

      if (moon) {
        ctx.save()
        ctx.translate(moon.x, moon.y)

        // Dimming for Partly Cloudy
        const isPartly = c.includes('partly')
        ctx.globalAlpha = isPartly ? 0.8 : 1.0

        // Glow: Cooler white/blue
        const grad = ctx.createRadialGradient(0, 0, moon.radius * 0.8, 0, 0, moon.radius * 4)
        grad.addColorStop(0, 'rgba(220, 230, 255, 0.25)')
        grad.addColorStop(1, 'rgba(220, 230, 255, 0)')
        ctx.fillStyle = grad
        ctx.beginPath()
        ctx.arc(0, 0, moon.radius * 4, 0, Math.PI * 2)
        ctx.fill()

        // Body: Radial gradient for sphere effect
        const bodyGrad = ctx.createRadialGradient(-8, -8, 2, 0, 0, moon.radius)
        bodyGrad.addColorStop(0, '#FFFFFF')        // Highlight
        bodyGrad.addColorStop(1, '#C8C8D8')        // Shadowy base
        ctx.fillStyle = bodyGrad

        ctx.shadowColor = 'rgba(255, 255, 255, 0.6)'
        ctx.shadowBlur = 12
        ctx.beginPath()
        ctx.arc(0, 0, moon.radius, 0, Math.PI * 2)
        ctx.fill()
        ctx.shadowBlur = 0

        // Craters with depth
        ctx.fillStyle = 'rgba(160, 160, 180, 0.5)'
        moon.craters.forEach(c => {
          ctx.beginPath()
          ctx.arc(c.x, c.y, c.r, 0, Math.PI * 2)
          ctx.fill()

          // Subtle highlight on crater rim
          ctx.strokeStyle = 'rgba(255, 255, 255, 0.4)'
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.arc(c.x, c.y, c.r, 0.8 * Math.PI, 1.8 * Math.PI)
          ctx.stroke()
        })

        ctx.restore()
      }

      // Snow Floor
      if (typeof snowfall === 'number' && snowfall > 0) {
        const mountHeight = Math.min(60, snowfall * 10) // Cap height

        ctx.fillStyle = "rgba(255, 255, 255, 0.9)"
        ctx.beginPath()
        ctx.moveTo(0, height)
        ctx.quadraticCurveTo(width * 0.2, height - mountHeight, width * 0.5, height - (mountHeight * 0.8))
        ctx.quadraticCurveTo(width * 0.8, height - (mountHeight * 0.6), width, height)
        ctx.lineTo(0, height)
        ctx.fill()
      }

      // 2. Clouds (Semi-transparent, so they obscure the sun/moon slightly)
      clouds.forEach(c => {
        c.x -= c.speed
        if (c.x < -100) c.x = width + 100

        ctx.beginPath()
        const grad = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, c.size)
        // Night clouds are darker
        const baseColor = isDay ? "255, 255, 255" : "180, 190, 200"
        grad.addColorStop(0, `rgba(${baseColor}, ${c.opacity})`)
        grad.addColorStop(1, `rgba(${baseColor}, 0)`)
        ctx.fillStyle = grad
        ctx.arc(c.x, c.y, c.size, 0, Math.PI * 2)
        ctx.arc(c.x + c.size * 0.6, c.y + c.size * 0.2, c.size * 0.7, 0, Math.PI * 2)
        ctx.fill()
      })

      // 3. Birds
      if (activeBird) {
        if (birds.length < (isPartly ? 3 : 1) && Math.random() > 0.995) {
          birds.push({
            x: -30,
            y: 30 + Math.random() * 60,
            speed: 0.7 + Math.random() * 0.3,
            flapPhase: 0,
            flapSpeed: 0.08,
            glideTimer: 0,
            isGliding: false,
            size: 0.6 + Math.random() * 0.3
          })
        }

        birds.forEach((b, index) => {
          b.x += b.speed

          if (b.isGliding) {
            b.glideTimer++
            b.y -= 0.05
            const targetPos = Math.PI * 0.5
            b.flapPhase = b.flapPhase + (targetPos - b.flapPhase) * 0.05
            if (b.glideTimer > 150) {
              b.isGliding = false
              b.glideTimer = 0
            }
          } else {
            b.flapPhase += b.flapSpeed
            b.y += Math.sin(b.flapPhase) * 0.2
            if (Math.sin(b.flapPhase) > 0.9 && Math.random() > 0.95) {
              b.isGliding = true
            }
          }

          if (b.x > width + 50) {
            birds.splice(index, 1)
            return
          }

          const wingSpan = 14 * b.size
          const wingY = Math.cos(b.flapPhase) * 5 * b.size

          ctx.fillStyle = "rgba(255, 255, 255, 0.95)"
          ctx.shadowColor = "rgba(255, 255, 255, 0.5)"
          ctx.shadowBlur = 10
          ctx.beginPath()
          ctx.moveTo(b.x + (4 * b.size), b.y)
          ctx.bezierCurveTo(b.x, b.y, b.x - (wingSpan * 0.5), b.y - wingY - (4 * b.size), b.x - wingSpan, b.y - wingY)
          ctx.quadraticCurveTo(b.x - (wingSpan * 0.5), b.y + (2 * b.size), b.x - (2 * b.size), b.y + (2 * b.size))
          ctx.quadraticCurveTo(b.x + (wingSpan * 0.5), b.y + (2 * b.size), b.x + wingSpan, b.y - wingY)
          ctx.bezierCurveTo(b.x + (wingSpan * 0.5), b.y - wingY - (4 * b.size), b.x + (2 * b.size), b.y, b.x + (4 * b.size), b.y)
          ctx.fill()
          ctx.shadowBlur = 0
        })
      }

      // 4. Particles (Rain/Snow/Stars)
      particles.forEach(p => {
        if (activeRain) {
          p.y += p.speed
          p.x -= 0.5

          // Splash Logic
          if (p.y > height - 5) {
            // Spawn Splash droplets
            const splashCount = Math.floor(Math.random() * 3) + 2
            for (let k = 0; k < splashCount; k++) {
              splashes.push({
                x: p.x,
                y: height - 2,
                vx: (Math.random() - 0.5) * 4, // Spread out
                vy: -(Math.random() * 3 + 1), // Jump up
                life: 1.0,
                maxLife: 20 + Math.random() * 10
              })
            }
            // Reset Raindrop
            p.y = -20
            p.x = Math.random() * width + 20
          }

          ctx.beginPath()
          ctx.strokeStyle = `rgba(255, 255, 255, ${0.2 * p.z})`
          ctx.lineWidth = 1.2
          const trailLen = p.speed * 1.8
          ctx.moveTo(p.x, p.y)
          ctx.lineTo(p.x - 1, p.y - trailLen)
          ctx.stroke()
        }
        else if (activeSnow) {
          p.y += p.speed
          p.x += Math.sin((p.y * 0.01) + p.swayOffset) * 0.3
          if (p.y > height) { p.y = -5; p.x = Math.random() * width }

          ctx.beginPath()

          let pAlpha = 0.8 * p.z
          let pColorStart = "255, 255, 255"

          // --- SNOW LIGHT ILLUMINATION ---
          // If street light is active, check recycled/calculated lamp position
          // Lamp is roughly at width-65-28 approx width-93, height-135-16 approx height-150
          // Simple proximity check for nice effect
          if (activeStreetLight) {
            const lampX = width - 93
            const lampY = height - 150
            const dx = p.x - lampX
            const dy = p.y - lampY
            const dist = Math.sqrt(dx * dx + dy * dy)

            // If within light cone range (approx check)
            if (dist < 180 && p.y > lampY) {
              pColorStart = "255, 220, 150" // Warm tint
              pAlpha = Math.min(1, pAlpha + 0.2) // Boost visibility
            }
          }

          const gradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, 2 * p.z)
          gradient.addColorStop(0, `rgba(${pColorStart}, ${pAlpha})`)
          gradient.addColorStop(1, `rgba(${pColorStart}, 0)`)
          ctx.fillStyle = gradient
          ctx.arc(p.x, p.y, 2 * p.z, 0, Math.PI * 2)
          ctx.fill()
        }
        else if (activeStars) {
          p.opacity += p.twinkle
          if (p.opacity > 0.9 || p.opacity < 0.2) p.twinkle *= -1

          ctx.beginPath()
          ctx.fillStyle = `rgba(255, 255, 255, ${p.opacity})`
          ctx.arc(p.x, p.y, 1, 0, Math.PI * 2)
          ctx.fill()
        }
        else if (activeFog) {
          p.x += 0.5
          if (p.x > width) p.x = -50
          ctx.beginPath()
          const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, 60 * p.z)
          grad.addColorStop(0, `rgba(255, 255, 255, ${0.05 * p.z})`)
          grad.addColorStop(1, 'rgba(255, 255, 255, 0)')
          ctx.fillStyle = grad
          ctx.rect(0, 0, width, height)
          ctx.fill()
        }
      })

      // 5. Render Splashes (Only active if raining)
      if (activeRain) {
        for (let i = splashes.length - 1; i >= 0; i--) {
          const s = splashes[i]

          // Physics
          s.x += s.vx
          s.y += s.vy
          s.vy += 0.2 // Gravity

          s.life -= 1.5 // Fade out speed

          if (s.life <= 0 || s.y > height) {
            splashes.splice(i, 1)
            continue
          }

          const alpha = s.life / s.maxLife
          ctx.fillStyle = `rgba(200, 220, 255, ${alpha * 0.6})`
          ctx.beginPath()
          ctx.arc(s.x, s.y, 1.5, 0, Math.PI * 2)
          ctx.fill()
        }
      }

      animationId = requestAnimationFrame(draw)
    }

    if (activeRain || activeSnow || activeStars || activeFog || activeBird || clouds.length > 0 || activeSun || activeMoon) {
      draw()
    } else {
      const width = canvas.width / (window.devicePixelRatio || 1)
      const height = canvas.height / (window.devicePixelRatio || 1)
      ctx.clearRect(0, 0, width, height)
    }

    return () => {
      cancelAnimationFrame(animationId)
      resizeObserver.disconnect()
    }
  }, [condition, isDay])

  return (
    <div className={className} style={{
      position: 'absolute',
      inset: 0,
      zIndex: 0,
      background: 'transparent',
      pointerEvents: 'none'
    }}>
      <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
    </div>
  )
}