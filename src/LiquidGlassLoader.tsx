import './LiquidGlassLoader.css'

export default function LiquidGlassLoader() {
    return (
        <div className="liquid-loader">
            {/* Main sphere body — everything lives INSIDE */}
            <div className="cosmic-sphere">
                {/* 3D depth curvature layer */}
                <div className="sphere-depth"></div>

                {/* Internal nebula flow — the soul of the animation */}
                <div className="nebula-flow">
                    <div className="nebula-patch p1"></div>
                    <div className="nebula-patch p2"></div>
                    <div className="nebula-patch p3"></div>
                    <div className="nebula-patch p4"></div>
                </div>

                {/* Milky way streaks — thin luminous wisps flowing across */}
                <div className="milky-streak mk1"></div>
                <div className="milky-streak mk2"></div>
                <div className="milky-streak mk3"></div>

                {/* Orbital rings — INSIDE the sphere like trapped light arcs */}
                <div className="orbit-ring ring-1"></div>
                <div className="orbit-ring ring-2"></div>

                {/* Internal micro-stars — trapped light sparks */}
                <div className="inner-star s1"></div>
                <div className="inner-star s2"></div>
                <div className="inner-star s3"></div>
                <div className="inner-star s4"></div>
                <div className="inner-star s5"></div>

                {/* Specular caustic highlight */}
                <div className="sphere-specular"></div>

                {/* Surface glass sheen */}
                <div className="sphere-sheen"></div>
            </div>

            {/* Ambient aura — only the glow is external */}
            <div className="loader-glow"></div>
        </div>
    )
}
