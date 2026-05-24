import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { ACTIVITY_TYPE_COLORS, DATE_PALETTE, MUTED_ROUTE, STRAVA_ORANGE, withAlpha } from "../lib/colors";
import PlaybackControls from "./PlaybackControls";

const STADIA_KEY = import.meta.env.VITE_STADIA_MAPS_API_KEY || "";
const MAP_STYLE = {
  version: 8,
  sources: {
    stadia: {
      type: "raster",
      tiles: [
        `https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png${STADIA_KEY ? "?api_key=" + STADIA_KEY : ""}`
      ],
      tileSize: 256,
      attribution: "© <a href='https://stadiamaps.com/'>Stadia Maps</a> © <a href='https://openmaptiles.org/'>OpenMapTiles</a> © <a href='https://openstreetmap.org/copyright'>OpenStreetMap</a>"
    }
  },
  layers: [{ id: "stadia-bg", type: "raster", source: "stadia" }]
};
const FALLBACK_CENTER = [103.8198, 1.3521];

export default function MapCanvas({
  mapMode,
  playback,
  currentTime,
  trailMode,
  loading,
  error,
  isPlaying,
  dayStart,
  dayEnd,
  onPlayToggle,
  onReset,
  onScrub,
  speed,
  onSpeedChange,
  onTrailModeChange,
  timelineDensity,
  selectedAthlete,
  highlightedAthleteId
}) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const popupRef = useRef(null);
  const lastFitKeyRef = useRef("");
  const deferredTime = useDeferredValue(currentTime);
  const [mapReady, setMapReady] = useState(false);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return;
    }

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: FALLBACK_CENTER,
      zoom: 10,
      attributionControl: true
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");

    map.on("load", () => {
      setMapReady(true);
      quietBaseMap(map);
      map.addSource("trails", { type: "geojson", data: emptyFeatureCollection() });
      map.addSource("positions", { type: "geojson", data: emptyFeatureCollection() });
      map.addSource("privacy-points", { type: "geojson", data: emptyFeatureCollection() });
      map.addSource("activity-flags", { type: "geojson", data: emptyFeatureCollection() });

      map.addLayer({
        id: "trail-lines-halo",
        type: "line",
        source: "trails",
        paint: {
          "line-color": "rgba(8, 12, 18, 0.92)",
          "line-width": 8.5,
          "line-opacity": 0.94
        }
      });

      map.addLayer({
        id: "trail-lines",
        type: "line",
        source: "trails",
        paint: {
          "line-color": ["get", "color"],
          "line-width": 5.5,
          "line-opacity": 0.98
        }
      });

      map.addLayer({
        id: "position-points",
        type: "circle",
        source: "positions",
        paint: {
          "circle-color": ["get", "color"],
          "circle-radius": 6.5,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff"
        }
      });

      map.addLayer({
        id: "privacy-points-layer",
        type: "circle",
        source: "privacy-points",
        paint: {
          "circle-color": ["get", "color"],
          "circle-radius": 9,
          "circle-opacity": 0.26,
          "circle-stroke-width": 2.5,
          "circle-stroke-color": ["get", "color"]
        }
      });

      map.addLayer({
        id: "privacy-labels-layer",
        type: "symbol",
        source: "privacy-points",
        layout: {
          "text-field": ["get", "label"],
          "text-size": 11,
          "text-offset": [0, 1.4],
          "text-anchor": "top"
        },
        paint: {
          "text-color": ["get", "color"],
          "text-halo-color": "#08111b",
          "text-halo-width": 1.6
        }
      });

      map.addLayer({
        id: "activity-flag-badges",
        type: "circle",
        source: "activity-flags",
        paint: {
          "circle-color": ["get", "badgeFill"],
          "circle-radius": 11,
          "circle-stroke-width": 2,
          "circle-stroke-color": ["get", "badgeStroke"]
        }
      });

      map.addLayer({
        id: "activity-flag-labels",
        type: "symbol",
        source: "activity-flags",
        layout: {
          "text-field": ["get", "label"],
          "text-size": 10,
          "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
          "text-anchor": "center"
        },
        paint: {
          "text-color": ["get", "textColor"],
          "text-halo-color": "#08111b",
          "text-halo-width": 0.8
        }
      });

      const interactiveLayers = ["trail-lines", "position-points", "privacy-points-layer", "activity-flag-badges"];
      for (const layerId of interactiveLayers) {
        map.on("mouseenter", layerId, () => {
          map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", layerId, () => {
          map.getCanvas().style.cursor = "";
        });
        map.on("click", layerId, (event) => {
          const feature = event.features?.[0];
          if (!feature) {
            return;
          }
          openPopup(map, popupRef, feature, event.lngLat.toArray());
        });
      }

      map.on("click", (event) => {
        const hit = map.queryRenderedFeatures(event.point, { layers: interactiveLayers });
        if (!hit.length) {
          popupRef.current?.remove();
        }
      });
    });

    mapRef.current = map;
    return () => {
      popupRef.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  const geojson = useMemo(
    () => buildPlaybackFeatures(playback, deferredTime, trailMode, mapMode, highlightedAthleteId),
    [playback, deferredTime, trailMode, mapMode, highlightedAthleteId]
  );

  const playbackBounds = useMemo(() => buildPlaybackBounds(playback?.trips), [playback?.trips]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapReady || !map.isStyleLoaded()) {
      return;
    }

    map.getSource("trails")?.setData(geojson.trails);
    map.getSource("positions")?.setData(geojson.positions);
    map.getSource("privacy-points")?.setData(geojson.privacyPoints);
    map.getSource("activity-flags")?.setData(geojson.activityFlags);
  }, [geojson, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    const fitKey = mapMode === "athlete"
      ? `athlete:${selectedAthlete?.athlete_id ?? "none"}`
      : `date:${playback?.date ?? "none"}`;

    if (!map || !mapReady || !map.isStyleLoaded() || !playbackBounds) {
      return;
    }
    if (lastFitKeyRef.current === fitKey) {
      return;
    }

    map.fitBounds(playbackBounds, {
      padding: { top: 92, right: 56, bottom: 132, left: 56 },
      duration: 0
    });
    lastFitKeyRef.current = fitKey;
  }, [mapMode, selectedAthlete?.athlete_id, playback?.date, playbackBounds, mapReady]);

  return (
    <section className="mapPanel">
      <div ref={containerRef} className="mapCanvas" />

      <div className="mapHeader">
        <div className="mapTitleCard">
          <h1>{mapMode === "athlete" ? selectedAthlete?.name ?? "Athlete view" : playback?.date ?? "Date playback"}</h1>
          <p className="mapSubhead">
            {mapMode === "athlete"
              ? `${playback?.trips?.length ?? 0} saved routes`
              : `${playback?.athlete_count ?? 0} athletes`}
          </p>
        </div>
        {loading || error ? (
          <div className="mapStatusCard">{loading ? "Loading saved routes..." : error}</div>
        ) : null}
      </div>

      <div className="mapDock">
        <PlaybackControls
          currentTime={currentTime}
          dayStart={dayStart}
          dayEnd={dayEnd}
          isPlaying={isPlaying}
          onPlayToggle={onPlayToggle}
          onReset={onReset}
          onScrub={onScrub}
          speed={speed}
          onSpeedChange={onSpeedChange}
          trailMode={trailMode}
          onTrailModeChange={onTrailModeChange}
          timelineDensity={timelineDensity}
        />
      </div>
    </section>
  );
}

function buildPlaybackFeatures(playback, currentTime, trailMode, mapMode, highlightedAthleteId) {
  if (!playback || currentTime == null) {
    return {
      trails: emptyFeatureCollection(),
      positions: emptyFeatureCollection(),
      privacyPoints: emptyFeatureCollection(),
      activityFlags: emptyFeatureCollection()
    };
  }

  const trailFeatures = [];
  const positionFeatures = [];
  const privacyPointFeatures = [];
  const activityFlags = [];
  const athleteOrder = buildAthleteRouteOrder(playback.trips ?? []);

  for (const trip of playback.trips ?? []) {
    const path = trip.path ?? [];
    const startUnix = trip.start_unix ?? (path[0]?.[2] ?? null);
    const endUnix = trip.end_unix ?? (path[path.length - 1]?.[2] ?? startUnix);
    if (startUnix != null && currentTime < startUnix) {
      continue;
    }

    const color = colorForTrip(trip, mapMode, highlightedAthleteId);
    const sharedProperties = buildFeatureProperties(trip, color);

    if (path.length) {
      const visiblePoints = path.filter((point) => point[2] <= currentTime);

      if (trailMode === "persist" && visiblePoints.length > 1) {
        trailFeatures.push({
          type: "Feature",
          properties: sharedProperties,
          geometry: {
            type: "LineString",
            coordinates: visiblePoints.map((point) => [point[0], point[1]])
          }
        });
      }

      const currentPoint = findCurrentPoint(path, currentTime);
      if (currentPoint) {
        positionFeatures.push({
          type: "Feature",
          properties: sharedProperties,
          geometry: {
            type: "Point",
            coordinates: [currentPoint[0], currentPoint[1]]
          }
        });
      }
    }

    if (startUnix != null && currentTime >= startUnix) {
      privacyPointFeatures.push(...buildPrivacyMarkers(trip, color, sharedProperties));
      if (mapMode === "athlete") {
        activityFlags.push(...buildActivityFlags(trip, color, sharedProperties, currentTime, athleteOrder));
      }
    }
  }

  return {
    trails: { type: "FeatureCollection", features: trailFeatures },
    positions: { type: "FeatureCollection", features: positionFeatures },
    privacyPoints: { type: "FeatureCollection", features: privacyPointFeatures },
    activityFlags: { type: "FeatureCollection", features: activityFlags }
  };
}

function buildFeatureProperties(trip, color) {
  const startUnix = trip.start_unix ?? null;
  const endUnix = trip.end_unix ?? null;
  return {
    activityId: String(trip.activity_id ?? ""),
    athleteId: String(trip.athlete_id ?? ""),
    athleteName: trip.athlete_name ?? "Unknown athlete",
    activityName: trip.activity_name ?? "Untitled activity",
    sportType: trip.sport_type ?? "Activity",
    activityDate: trip.calendar_date ?? formatDateOnly(startUnix),
    startLabel: formatDateTime(startUnix),
    endLabel: formatDateTime(endUnix),
    durationLabel: formatDuration(startUnix, endUnix),
    privacyWarning:
      trip.privacy_zone_start || trip.privacy_zone_end ? "Route partially hidden by privacy zone" : "",
    color
  };
}

function buildPlaybackBounds(routes) {
  if (!routes?.length) {
    return null;
  }

  const bounds = new maplibregl.LngLatBounds();
  let hasBounds = false;

  for (const trip of routes) {
    for (const point of trip.path ?? []) {
      bounds.extend([point[0], point[1]]);
      hasBounds = true;
    }
    if (trip.truncation_point_start) {
      bounds.extend(trip.truncation_point_start);
      hasBounds = true;
    }
    if (trip.truncation_point_end) {
      bounds.extend(trip.truncation_point_end);
      hasBounds = true;
    }
  }

  return hasBounds ? bounds : null;
}

function findCurrentPoint(path, currentTime) {
  let latest = null;
  for (const point of path) {
    if (point[2] > currentTime) {
      break;
    }
    latest = point;
  }
  return latest;
}

function buildPrivacyMarkers(trip, color, sharedProperties) {
  const markers = [];
  if (trip.privacy_zone_start && trip.truncation_point_start) {
    markers.push({
      type: "Feature",
      properties: { ...sharedProperties, label: "TRUNCATED" },
      geometry: { type: "Point", coordinates: trip.truncation_point_start }
    });
  }
  if (trip.privacy_zone_end && trip.truncation_point_end) {
    markers.push({
      type: "Feature",
      properties: { ...sharedProperties, label: "TRUNCATED" },
      geometry: { type: "Point", coordinates: trip.truncation_point_end }
    });
  }
  return markers;
}

function buildActivityFlags(trip, color, sharedProperties, currentTime, orderMap) {
  const features = [];
  const path = trip.path ?? [];
  const sequence = orderMap.get(trip.activity_id) ?? 0;
  const startCoordinates = trip.truncation_point_start ?? (path[0] ? [path[0][0], path[0][1]] : null);
  const endCoordinates = trip.truncation_point_end ?? (path[path.length - 1] ? [path[path.length - 1][0], path[path.length - 1][1]] : null);
  const startUnix = trip.start_unix ?? path[0]?.[2] ?? null;
  const endUnix = trip.end_unix ?? path[path.length - 1]?.[2] ?? startUnix;

  if (startCoordinates && startUnix != null && currentTime >= startUnix) {
    features.push({
      type: "Feature",
      properties: {
        ...sharedProperties,
        label: `${sequence}S`,
        badgeFill: "#08111b",
        badgeStroke: color,
        textColor: "#ffffff"
      },
      geometry: { type: "Point", coordinates: startCoordinates }
    });
  }

  if (endCoordinates && endUnix != null && currentTime >= endUnix) {
    features.push({
      type: "Feature",
      properties: {
        ...sharedProperties,
        label: `${sequence}E`,
        badgeFill: withAlpha(color, 0.28),
        badgeStroke: color,
        textColor: "#ffffff"
      },
      geometry: { type: "Point", coordinates: endCoordinates }
    });
  }

  return features;
}

function buildAthleteRouteOrder(trips) {
  const orderedTrips = [...trips]
    .filter((trip) => trip.start_unix != null)
    .sort((left, right) => (left.start_unix ?? 0) - (right.start_unix ?? 0));
  return new Map(orderedTrips.map((trip, index) => [trip.activity_id, index + 1]));
}

function colorForTrip(trip, mapMode, highlightedAthleteId) {
  if (mapMode === "athlete") {
    return ACTIVITY_TYPE_COLORS[trip.sport_type] ?? "#D6A15C";
  }

  if (highlightedAthleteId != null) {
    return trip.athlete_id === highlightedAthleteId ? STRAVA_ORANGE : MUTED_ROUTE;
  }

  return DATE_PALETTE[Math.abs(trip.athlete_id ?? 0) % DATE_PALETTE.length];
}

function openPopup(map, popupRef, feature, fallbackCoordinates) {
  const coordinates = feature.geometry?.type === "Point"
    ? feature.geometry.coordinates.slice()
    : fallbackCoordinates;

  const popup = popupRef.current ?? new maplibregl.Popup({ closeButton: true, closeOnClick: false, maxWidth: "320px" });
  popupRef.current = popup;
  popup.setLngLat(coordinates).setHTML(renderPopup(feature.properties)).addTo(map);
}

function renderPopup(properties = {}) {
  const warning = properties.privacyWarning
    ? `<p class="popupWarning">${escapeHtml(properties.privacyWarning)}</p>`
    : "";

  return `
    <div class="mapPopup">
      <div class="mapPopupHeader">
        <strong>${escapeHtml(properties.athleteName ?? "Unknown athlete")}</strong>
        <span>${escapeHtml(properties.sportType ?? "Activity")}</span>
      </div>
      <p class="mapPopupTitle">${escapeHtml(properties.activityName ?? "Untitled activity")}</p>
      <div class="mapPopupMeta">
        <span><strong>Date</strong>${escapeHtml(properties.activityDate ?? "-")}</span>
        <span><strong>Start</strong>${escapeHtml(properties.startLabel ?? "-")}</span>
        <span><strong>End</strong>${escapeHtml(properties.endLabel ?? "-")}</span>
        <span><strong>Duration</strong>${escapeHtml(properties.durationLabel ?? "-")}</span>
      </div>
      ${warning}
    </div>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDateOnly(unixSeconds) {
  if (!unixSeconds) {
    return "-";
  }
  return new Date(unixSeconds * 1000).toLocaleDateString("en-SG", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: "Asia/Singapore"
  });
}

function formatDateTime(unixSeconds) {
  if (!unixSeconds) {
    return "-";
  }
  return new Date(unixSeconds * 1000).toLocaleString("en-SG", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Singapore"
  });
}

function formatDuration(startUnix, endUnix) {
  if (!startUnix || !endUnix || endUnix < startUnix) {
    return "-";
  }
  const total = endUnix - startUnix;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  return `${minutes}m ${seconds}s`;
}

function emptyFeatureCollection() {
  return { type: "FeatureCollection", features: [] };
}

function quietBaseMap(map) {
  const layers = map.getStyle()?.layers ?? [];
  for (const layer of layers) {
    if (!layer.id) {
      continue;
    }

    if (layer.type === "symbol") {
      map.setLayoutProperty(layer.id, "visibility", "none");
      continue;
    }
    if (layer.type === "line") {
      map.setPaintProperty(layer.id, "line-opacity", 0.1);
      continue;
    }
    if (layer.type === "fill") {
      map.setPaintProperty(layer.id, "fill-opacity", 0.14);
      continue;
    }
    if (layer.type === "fill-extrusion") {
      map.setPaintProperty(layer.id, "fill-extrusion-opacity", 0.04);
      continue;
    }
    if (layer.type === "circle") {
      map.setPaintProperty(layer.id, "circle-opacity", 0.08);
    }
  }
}
