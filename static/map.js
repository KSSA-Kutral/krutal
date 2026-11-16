const $ = id => document.getElementById(id);
const map = L.map("map").setView([-43.5, -68], 5);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors"
}).addTo(map);

const layers = {
  detections: L.layerGroup().addTo(map),
  combined: L.layerGroup().addTo(map),
  weather: L.layerGroup(),
  history: L.layerGroup(),
  stations: L.layerGroup(),
  water: L.layerGroup()
};

L.control.layers(null, {
  "Combined estimate": layers.combined,
  "Weather conditions": layers.weather,
  "Satellite observations": layers.detections,
  "Relative satellite activity": layers.history,
  "Fire stations": layers.stations,
  "Water-related objects": layers.water
}, { collapsed: false }).addTo(map);

let selectedStation = null;
let routeLine = null;
let infraBusy = false;
let lastInfraRequest = 0;
const INFRA_COOLDOWN = 30000;

function scoreColor(score) {
  return score < 20 ? "#fff3a3" :
         score < 40 ? "#ffd166" :
         score < 60 ? "#ff9f43" :
         score < 80 ? "#f04438" : "#a80000";
}

function clearDataLayers() {
  ["detections", "combined", "weather", "history"].forEach(name => layers[name].clearLayers());
}

function drawCell(layer, cell, score, title, extra) {
  L.rectangle([[cell.south, cell.west], [cell.north, cell.east]], {
    color: scoreColor(score), weight: 1, fillColor: scoreColor(score),
    fillOpacity: 0.15 + score / 100 * 0.48
  }).bindPopup(`<b>${title}</b><br>Score: ${score}/100<br>${extra}`)
    .addTo(layer);
}

function queryParams() {
  return new URLSearchParams({
    start: $("date-start").value,
    end: $("date-end").value,
    time_start: $("time-start").value || "00:00",
    time_end: $("time-end").value || "23:59"
  });
}

async function loadMapData() {
  $("map-status").textContent = "Loading…";
  $("data-note").textContent = "";
  clearDataLayers();

  try {
    const params = queryParams();
    const step = $("grid-step").value;
    const [hotspotResponse, gridResponse] = await Promise.all([
      fetch(`/api/hotspots?${params}`),
      fetch(`/api/risk-grid?${params}&step=${step}`)
    ]);
    const hotspotData = await hotspotResponse.json();
    const gridData = await gridResponse.json();
    if (!hotspotResponse.ok) throw new Error(hotspotData.error || "Could not load satellite data.");
    if (!gridResponse.ok) throw new Error(gridData.error || "Could not load environmental grid.");

    const observations = hotspotData.observations || [];
    $("count").textContent = hotspotData.count ?? observations.length;
    $("period").textContent = `${$("date-start").value} → ${$("date-end").value}`;
    $("data-note").textContent = `${hotspotData.note || ""} ${gridData.history_note || ""}`;

    observations.forEach(point => {
      if (point.latitude == null || point.longitude == null) return;
      const modis = String(point.sensor).toUpperCase().includes("MODIS");
      L.circleMarker([Number(point.latitude), Number(point.longitude)], {
        radius: 6, color: modis ? "#fb923c" : "#f43f5e",
        fillColor: modis ? "#fb923c" : "#f43f5e", fillOpacity: .88, weight: 1
      }).bindPopup(`
        <b>${point.sensor || "Satellite observation"}</b><br>
        Satellite: ${point.satellite || "Not specified"}<br>
        Date: ${point.acq_date || "—"}<br>
        Acquisition time: ${point.acq_time || "—"} UTC<br>
        FRP: ${point.frp ?? "—"} MW<br>
        Coordinates: ${point.latitude}, ${point.longitude}
      `).addTo(layers.detections);
    });

    (gridData.cells || []).forEach(cell => {
      if (cell.weather_score == null) return;
      drawCell(layers.weather, cell, cell.weather_score, "Weather conditions",
        `Temperature: ${cell.weather.temperature} °C<br>Humidity: ${cell.weather.humidity}%<br>Wind: ${cell.weather.wind_speed} km/h<br>Rain (24h): ${cell.weather.rain_last_24h} mm`);
      drawCell(layers.history, cell, cell.history_score, "Relative satellite activity",
        `Observations in selected period: ${cell.hotspot_count}<br>Relative score: ${cell.history_score}/100`);
      drawCell(layers.combined, cell, cell.combined_score, "Combined experimental estimate",
        `Weather score: ${cell.weather_score}/100<br>Relative satellite activity: ${cell.history_score}/100<br>Combined score: ${cell.combined_score}/100`);
    });

    $("map-status").textContent = "Map updated";
  } catch (error) {
    console.error(error);
    $("map-status").textContent = "Data unavailable";
    $("data-note").textContent = error.message;
  }
}

async function loadInfrastructure() {
  if (map.getZoom() < 7) {
    $("infrastructure-status").textContent = "Zoom in to load nearby mapped infrastructure.";
    return;
  }
  const now = Date.now();
  if (infraBusy || now - lastInfraRequest < INFRA_COOLDOWN) return;
  infraBusy = true;
  lastInfraRequest = now;
  $("infrastructure-status").textContent = "Loading OpenStreetMap objects…";

  const bounds = map.getBounds();
  const params = new URLSearchParams({
    south: bounds.getSouth(), west: bounds.getWest(),
    north: bounds.getNorth(), east: bounds.getEast()
  });

  try {
    const response = await fetch(`/api/infrastructure?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Infrastructure request failed.");
    layers.stations.clearLayers();
    layers.water.clearLayers();

    data.elements.forEach(place => {
      const station = place.type === "fire_station";
      const marker = L.circleMarker([place.latitude, place.longitude], {
        radius: station ? 8 : 5,
        color: station ? "#38bdf8" : "#2dd4bf",
        fillColor: station ? "#0284c7" : "#0f766e", fillOpacity: .9
      }).bindPopup(`<b>${place.name}</b><br>Type: ${place.type}<br>${place.operator || ""}`);

      if (station) {
        marker.on("click", () => {
          selectedStation = { lat: place.latitude, lon: place.longitude, name: place.name };
          $("infrastructure-status").textContent = `Route origin selected: ${place.name}. Click a destination on the map.`;
        }).addTo(layers.stations);
      } else marker.addTo(layers.water);
    });
    $("infrastructure-status").textContent = `${data.count} mapped objects loaded.`;
  } catch (error) {
    console.error(error);
    $("infrastructure-status").textContent = `Infrastructure unavailable: ${error.message}`;
  } finally {
    infraBusy = false;
  }
}

map.on("zoomend", loadInfrastructure);
map.on("click", async event => {
  if (!selectedStation) return;
  const params = new URLSearchParams({
    start_lat: selectedStation.lat, start_lon: selectedStation.lon,
    end_lat: event.latlng.lat, end_lon: event.latlng.lng
  });
  try {
    const response = await fetch(`/api/route?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "No route found.");
    if (routeLine) map.removeLayer(routeLine);
    routeLine = L.geoJSON(data.geometry, { style: { color: "#38bdf8", weight: 5 } }).addTo(map);
    L.popup().setLatLng(event.latlng).setContent(
      `<b>Road route estimate</b><br>From: ${selectedStation.name}<br>Distance: ${data.distance_km} km<br>Estimated drive: ${data.duration_minutes} min<br><small>Not an emergency response-time estimate.</small>`
    ).openOn(map);
  } catch (error) {
    alert(error.message);
  }
});

async function checkKey() {
  try {
    const response = await fetch("/api/key-status");
    const data = await response.json();
    $("key-status").textContent = data.configured
      ? "A FIRMS MAP_KEY is configured on this server."
      : "No MAP_KEY configured. The app may show its included sample dataset.";
  } catch {
    $("key-status").textContent = "Could not check key configuration.";
  }
}

$("save-key").addEventListener("click", async () => {
  const key = $("map-key").value.trim();
  if (!key) return alert("Paste your FIRMS MAP_KEY first.");
  $("save-key").disabled = true;
  $("save-key").textContent = "Saving…";
  try {
    const response = await fetch("/api/save-key", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save key.");
    $("map-key").value = "";
    await checkKey();
    alert("MAP_KEY saved to the server's .env file.");
  } catch (error) {
    alert(error.message);
  } finally {
    $("save-key").disabled = false;
    $("save-key").textContent = "Save key locally";
  }
});

$("refresh").addEventListener("click", loadMapData);
$("grid-step").addEventListener("change", loadMapData);

const today = new Date();
const yyyyMmDd = today.toISOString().slice(0, 10);
const fiveDaysAgo = new Date(today.getTime() - 4 * 86400000).toISOString().slice(0, 10);
$("date-start").value = fiveDaysAgo;
$("date-end").value = yyyyMmDd;

checkKey();
loadMapData();
