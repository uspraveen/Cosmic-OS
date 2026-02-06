import './LiquidGlassLoader.css'

export default function LiquidGlassLoader() {
    return (
        <div className="liquid-loader">
            <div className="loader-glass">
                <div className="morph-container">
                    <div className="morph-blob blob-1"></div>
                    <div className="morph-blob blob-2"></div>
                    <div className="morph-blob blob-3"></div>
                    <div className="morph-blob blob-4"></div>
                </div>
            </div>
            <div className="loader-glow"></div>
        </div>
    )
}
