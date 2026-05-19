import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

type MapMarker = {
  id: string
  label: string
  position: [number, number]
  color?: string | null
  kind?: string | null
  description?: string | null
}

type MapRoute = {
  id: string
  label: string
  color?: string | null
  width?: number | null
  geometry: {
    type: string
    coordinates: number[][]
  }
  distance_m?: number | null
  duration_s?: number | null
}

export type CosmicMapSpec = {
  version: number
  title: string
  subtitle?: string | null
  attribution?: string | null
  view?: {
    center?: [number, number]
    zoom?: number
    bounds?: {
      southwest: [number, number]
      northeast: [number, number]
    }
  }
  markers?: MapMarker[]
  routes?: MapRoute[]
}

type MapBounds = NonNullable<NonNullable<CosmicMapSpec['view']>['bounds']>

const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const TILE_ATTRIBUTION = '© OpenStreetMap contributors'

const isLngLat = (value: unknown): value is [number, number] => {
  if (!Array.isArray(value) || value.length < 2) return false
  return Number.isFinite(value[0]) && Number.isFinite(value[1])
}

const boundsHaveArea = (
  bounds: NonNullable<CosmicMapSpec['view']>['bounds'],
): bounds is MapBounds => {
  if (!bounds || !isLngLat(bounds.southwest) || !isLngLat(bounds.northeast)) return false
  return (
    Math.abs(bounds.northeast[0] - bounds.southwest[0]) > 0.00001 ||
    Math.abs(bounds.northeast[1] - bounds.southwest[1]) > 0.00001
  )
}

const invalidateMapSize = (map: L.Map) => {
  map.invalidateSize({ animate: false })
  window.requestAnimationFrame(() => {
    map.invalidateSize({ animate: false })
    window.setTimeout(() => map.invalidateSize({ animate: false }), 80)
  })
}

const formatDistance = (distanceM: number | null | undefined) => {
  if (!distanceM || distanceM <= 0) return null
  if (distanceM >= 1000) return `${(distanceM / 1000).toFixed(1)} km`
  return `${Math.round(distanceM)} m`
}

const formatDuration = (durationS: number | null | undefined) => {
  if (!durationS || durationS <= 0) return null
  const minutes = Math.round(durationS / 60)
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  return rem ? `${hours} hr ${rem} min` : `${hours} hr`
}

const InlineMap = ({
  title,
  subtitle,
  contentUrl,
}: {
  title: string
  subtitle?: string | null
  contentUrl: string
}) => {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [spec, setSpec] = useState<CosmicMapSpec | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setStatus('loading')
      setErrorMessage(null)
      try {
        const response = await fetch(contentUrl)
        if (!response.ok) {
          throw new Error(`Map data request failed (${response.status})`)
        }
        const payload = await response.json()
        if (!cancelled) {
          setSpec(payload as CosmicMapSpec)
          setStatus('ready')
        }
      } catch (error) {
        if (!cancelled) {
          setStatus('error')
          setErrorMessage(error instanceof Error ? error.message : 'Could not load map data.')
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [contentUrl])

  const routeSummary = useMemo(() => {
    const route = spec?.routes?.[0]
    if (!route) return null
    const distance = formatDistance(route.distance_m)
    const duration = formatDuration(route.duration_s)
    if (distance && duration) return `${distance} · ${duration}`
    return distance || duration
  }, [spec])

  useEffect(() => {
    if (status !== 'ready' || !spec || !containerRef.current) {
      return
    }

    if (mapRef.current) {
      mapRef.current.remove()
      mapRef.current = null
    }

    const firstMarkerPosition = spec.markers?.[0]?.position
    const center = isLngLat(spec.view?.center)
      ? spec.view.center
      : isLngLat(firstMarkerPosition)
        ? firstMarkerPosition
        : [0, 0]
    const zoom = spec.view?.zoom || 11
    const map = L.map(containerRef.current, {
      zoomControl: true,
      attributionControl: true,
      preferCanvas: true,
    }).setView([center[1], center[0]], zoom)

    L.tileLayer(TILE_URL, {
      maxZoom: 19,
      attribution: TILE_ATTRIBUTION,
    }).addTo(map)

    const layerGroup = L.layerGroup().addTo(map)

    for (const marker of spec.markers || []) {
      const [lng, lat] = marker.position
      const circle = L.circleMarker([lat, lng], {
        radius: 8,
        color: marker.color || '#2563eb',
        fillColor: marker.color || '#2563eb',
        fillOpacity: 0.9,
        weight: 2,
      })
      const popupLines = [marker.label]
      if (marker.description) popupLines.push(marker.description)
      circle.bindPopup(popupLines.join('<br/>'))
      circle.addTo(layerGroup)
    }

    for (const route of spec.routes || []) {
      const latLngs = (route.geometry?.coordinates || []).map(
        (coord) => [coord[1], coord[0]] as [number, number],
      )
      if (latLngs.length < 2) continue
      const polyline = L.polyline(latLngs, {
        color: route.color || '#2563eb',
        weight: route.width || 5,
        opacity: 0.9,
      })
      const popupLines = [route.label]
      const distance = formatDistance(route.distance_m)
      const duration = formatDuration(route.duration_s)
      if (distance) popupLines.push(distance)
      if (duration) popupLines.push(duration)
      polyline.bindPopup(popupLines.join('<br/>'))
      polyline.addTo(layerGroup)
    }

    const bounds = spec.view?.bounds
    const layers = layerGroup.getLayers()
    try {
      if (boundsHaveArea(bounds)) {
        map.fitBounds(
          [
            [bounds.southwest[1], bounds.southwest[0]],
            [bounds.northeast[1], bounds.northeast[0]],
          ],
          { padding: [24, 24] },
        )
      } else if (layers.length > 1) {
        map.fitBounds(L.featureGroup(layers).getBounds().pad(0.2))
      } else {
        map.setView([center[1], center[0]], zoom)
      }
    } catch {
      map.setView([center[1], center[0]], zoom)
    }

    mapRef.current = map
    invalidateMapSize(map)

    let resizeObserver: ResizeObserver | null = null
    if ('ResizeObserver' in window && containerRef.current) {
      resizeObserver = new ResizeObserver(() => invalidateMapSize(map))
      resizeObserver.observe(containerRef.current)
    }

    return () => {
      resizeObserver?.disconnect()
      map.remove()
      mapRef.current = null
    }
  }, [spec, status])

  return (
    <figure className="assistant-inline-map-card">
      <div className="assistant-inline-map-frame">
        {status === 'loading' && (
          <div className="assistant-inline-map-placeholder">Loading map…</div>
        )}
        {status === 'error' && (
          <div className="assistant-inline-map-placeholder">{errorMessage || 'Map unavailable'}</div>
        )}
        <div
          ref={containerRef}
          className={[
            'assistant-inline-map-canvas',
            status === 'ready' ? 'is-ready' : 'is-hidden',
          ].join(' ')}
        />
      </div>
      <figcaption className="assistant-inline-image-meta">
        <div className="assistant-inline-image-topline">
          <div className="assistant-inline-image-badge">INLINE MAP</div>
        </div>
        <div className="assistant-inline-image-name">{title}</div>
        {(subtitle || routeSummary) && (
          <div className="assistant-inline-image-subtitle">
            {[subtitle, routeSummary].filter(Boolean).join(' · ')}
          </div>
        )}
        <div className="assistant-inline-image-provenance">{TILE_ATTRIBUTION}</div>
      </figcaption>
    </figure>
  )
}

export default InlineMap
