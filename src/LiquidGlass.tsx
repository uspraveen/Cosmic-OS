import { useRef, useState, useEffect, useCallback, type CSSProperties, type ReactNode } from 'react'

interface LiquidGlassProps {
  children: ReactNode
  className?: string
  style?: CSSProperties
  cornerRadius?: number
  disableTilt?: boolean
}

export default function LiquidGlass({
  children,
  className = '',
  style = {},
  cornerRadius = 32,
  disableTilt = false,
}: LiquidGlassProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [mouse, setMouse] = useState({ x: 0.5, y: 0.5 })
  const [isHovered, setIsHovered] = useState(false)

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!containerRef.current || disableTilt) return
    const rect = containerRef.current.getBoundingClientRect()
    const x = (e.clientX - rect.left) / rect.width
    const y = (e.clientY - rect.top) / rect.height
    setMouse({ x, y })
  }, [disableTilt])

  useEffect(() => {
    if (disableTilt) return
    window.addEventListener('mousemove', handleMouseMove)
    return () => window.removeEventListener('mousemove', handleMouseMove)
  }, [handleMouseMove, disableTilt])

  const tiltX = disableTilt ? 0 : (mouse.x - 0.5) * 3
  const tiltY = disableTilt ? 0 : (mouse.y - 0.5) * -3

  return (
    <div
      ref={containerRef}
      className={`glass-root ${className}`}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      style={{
        position: 'relative',
        borderRadius: cornerRadius,
        transform: isHovered && !disableTilt
          ? `perspective(1000px) rotateX(${tiltY}deg) rotateY(${tiltX}deg) scale3d(1.01, 1.01, 1.01)` 
          : 'perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)',
        transition: 'transform 0.4s cubic-bezier(0.25, 0.46, 0.45, 0.94)',
        willChange: 'transform',
        ...style
      }}
    >
      {/* 1. Deep Glass Body (The "Physical" Material) */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          borderRadius: cornerRadius,
          background: 'rgba(20, 20, 22, 0.6)', // Deep dark tint
          backdropFilter: 'blur(32px) saturate(220%)', // Heavy blur + high saturation for "liquid" feel
          WebkitBackdropFilter: 'blur(32px) saturate(220%)',
          boxShadow: `
            0 25px 50px -12px rgba(0, 0, 0, 0.6), /* Drop Shadow */
            inset 0 1px 1px 0 rgba(255, 255, 255, 0.2), /* Top Inner Highlight */
            inset 0 -1px 1px 0 rgba(0, 0, 0, 0.4), /* Bottom Inner Shadow */
            inset 0 0 20px 0 rgba(0, 0, 0, 0.2) /* Inner Depth */
          `,
          border: '1px solid rgba(255, 255, 255, 0.08)' // Subtle physical border
        }}
      />

      {/* 2. Specular Surface Reflection (The "Wet" Look) */}
      <div
        style={{
          position: 'absolute',
          inset: 0,
          borderRadius: cornerRadius,
          background: `linear-gradient(
            135deg, 
            rgba(255, 255, 255, 0.15) 0%, 
            rgba(255, 255, 255, 0.02) 20%, 
            rgba(255, 255, 255, 0.0) 50%,
            rgba(255, 255, 255, 0.02) 80%,
            rgba(255, 255, 255, 0.08) 100%
          )`,
          pointerEvents: 'none',
          mixBlendMode: 'overlay'
        }}
      />

      {/* 3. Rim Light / Caustics (The "3D Edge" Pop) */}
      <div
        style={{
          position: 'absolute',
          inset: -1, // Sits slightly outside to hug the curve
          borderRadius: cornerRadius + 1,
          padding: 1.5, // Thickness of the rim
          background: `linear-gradient(
            ${140 + (tiltX * 10)}deg, 
            rgba(255, 255, 255, 0.5) 0%, 
            rgba(255, 255, 255, 0.1) 25%, 
            rgba(255, 255, 255, 0.0) 50%, 
            rgba(255, 255, 255, 0.1) 75%, 
            rgba(255, 255, 255, 0.4) 100%
          )`,
          mask: 'linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)',
          maskComposite: 'exclude',
          WebkitMask: 'linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)',
          WebkitMaskComposite: 'xor',
          pointerEvents: 'none',
          opacity: 0.9
        }}
      />

      {/* 4. Top "Curved" Shine (Simulates convex top edge) */}
      <div 
        style={{
          position: 'absolute',
          top: 0, left: '15%', right: '15%', height: '1px',
          background: 'linear-gradient(90deg, transparent, rgba(255,255,255,0.6), transparent)',
          opacity: 0.5,
          pointerEvents: 'none'
        }}
      />

      {/* 5. Content Container (Must fill height for scrolling!) */}
      <div style={{ position: 'relative', zIndex: 10, height: '100%', width: '100%' }}>
        {children}
      </div>
    </div>
  )
}