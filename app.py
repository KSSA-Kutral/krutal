import os
import time
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"
DATA_FILE = BASE / "data" / "combined_2024-01-15.csv"

load_dotenv(ENV_FILE)
FIRMS_KEY = os.getenv("FIRMS_MAP_KEY", "")

# Patagonia bounding box: west, south, east, north.
BBOX = "-76,-56,-63,-38"
FIRMS_SOURCES = ("MODIS_SP", "VIIRS_SNPP_SP")
weather_cache = {}
hotspot_cache = {}


def clamp(value, low, high):
    return max(low, min(value, high))


def weather_score(temp, humidity, wind, rain):
    t = clamp((temp - 15), 0, 20)
    h = clamp((60 - humidity) / 50 * 30, 0, 30)
    w = clamp(wind / 50 * 25, 0, 25)
    r = 25 if rain <= 1 else 20 if rain <= 3 else 12 if rain <= 8 else 5 if rain <= 15 else 0
    score = round(t + h + w + r)
    level = "Low" if score < 25 else "Moderate" if score < 50 else "High" if score < 75 else "Very High"
    return {"score": score, "level": level}


def parse_period():
    end = date.fromisoformat(request.args.get("end", date.today().isoformat()))
    start = date.fromisoformat(request.args.get("start", (end - timedelta(days=4)).isoformat()))
    if start > end:
        raise ValueError("Start date must be before or equal to end date.")
    days = (end - start).days + 1
    if days > 30:
        raise ValueError("Choose a maximum of 30 days at a time.")
    return start, end


def apply_time_filter(df):
    start = request.args.get("time_start", "00:00")
    end = request.args.get("time_end", "23:59")
    if "acq_time" not in df or df.empty:
        return df
    times = df["acq_time"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(4)
    minutes = pd.to_numeric(times.str[:2], errors="coerce") * 60 + pd.to_numeric(times.str[2:4], errors="coerce")
    try:
        a = int(start[:2]) * 60 + int(start[3:5])
        b = int(end[:2]) * 60 + int(end[3:5])
    except (ValueError, TypeError):
        raise ValueError("Invalid time range.")
    mask = (minutes >= a) & (minutes <= b) if a <= b else ((minutes >= a) | (minutes <= b))
    return df[mask.fillna(False)].copy()


def get_hotspots(start, end):
    """Fetch standard-processing FIRMS data in chunks of at most five days."""
    key = (start.isoformat(), end.isoformat())
    if key in hotspot_cache and time.time() - hotspot_cache[key]["time"] < 600:
        return hotspot_cache[key]["data"].copy(), hotspot_cache[key]["note"]

    if not FIRMS_KEY:
        if not DATA_FILE.exists():
            return pd.DataFrame(), "Add a FIRMS MAP_KEY to load live satellite data."
        df = pd.read_csv(DATA_FILE, dtype={"acq_time": str})
        note = "Using the included sample CSV. Add a FIRMS MAP_KEY to query the selected dates."
    else:
        frames = []
        for source in FIRMS_SOURCES:
            current = start
            while current <= end:
                span = min(5, (end - current).days + 1)
                url = (
                    "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
                    f"{FIRMS_KEY}/{source}/{BBOX}/{span}/{current.isoformat()}"
                )
                response = requests.get(url, timeout=45)
                response.raise_for_status()
                frame = pd.read_csv(StringIO(response.text), dtype={"acq_time": str})
                if not frame.empty:
                    frame["sensor"] = "MODIS" if source.startswith("MODIS") else "VIIRS"
                    frame["source"] = source
                    frames.append(frame)
                current += timedelta(days=span)

        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        note = "NASA FIRMS standard-processing observations. Acquisition times are UTC."

    if not df.empty:
        for column in ("latitude", "longitude", "frp"):
            if column in df:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        # The bundled CSV is a fixed demonstration sample. Its dates may not
        # match the selected live-query period, so only date-filter API data.
        if FIRMS_KEY and "acq_date" in df:
            df = df[df["acq_date"].between(start.isoformat(), end.isoformat())]
        df = df.dropna(subset=["latitude", "longitude"])
        if "sensor" not in df:
            df["sensor"] = df.get("instrument", "Unknown")
        if "satellite" not in df:
            df["satellite"] = "Not specified"

    hotspot_cache[key] = {"time": time.time(), "data": df.copy(), "note": note}
    return df, note


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/key-status")
def key_status():
    return jsonify({"configured": bool(FIRMS_KEY)})


@app.route("/api/save-key", methods=["POST"])
def save_key():
    global FIRMS_KEY
    # This convenience form is intended for local development only.
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "For a hosted app, configure FIRMS_MAP_KEY in the server environment instead."}), 403
    key = (request.json or {}).get("key", "").strip()
    if not key or len(key) > 200:
        return jsonify({"error": "Enter a valid MAP_KEY."}), 400
    set_key(str(ENV_FILE), "FIRMS_MAP_KEY", key)
    FIRMS_KEY = key
    hotspot_cache.clear()
    return jsonify({"saved": True, "configured": True})


@app.route("/api/hotspots")
def hotspots():
    try:
        start, end = parse_period()
        df, note = get_hotspots(start, end)
        df = apply_time_filter(df)
        df = df.astype(object).where(pd.notna(df), None)
        return jsonify({
            "observations": df.to_dict(orient="records"),
            "count": len(df),
            "note": note,
            "time_zone": "UTC"
        })
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        app.logger.exception("FIRMS request failed")
        return jsonify({"error": "NASA FIRMS could not be reached. Check your MAP_KEY and try again.", "details": str(error)}), 502


@app.route("/api/risk-grid")
def risk_grid():
    try:
        step = float(request.args.get("step", 1))
        if step not in (0.25, 0.5, 1, 2):
            return jsonify({"error": "Unsupported grid resolution."}), 400

        start, end = parse_period()
        history, history_note = get_hotspots(start, end)
        history = apply_time_filter(history)

        south, north, west, east = -56, -38, -76, -63
        cells = []
        lat = south
        while lat < north:
            lon = west
            while lon < east:
                s, n = lat, min(lat + step, north)
                w, e = lon, min(lon + step, east)
                cells.append({
                    "south": s, "north": n, "west": w, "east": e,
                    "lat": (s + n) / 2, "lon": (w + e) / 2
                })
                lon += step
            lat += step

        if not history.empty:
            for cell in cells:
                matches = history[
                    history["latitude"].between(cell["south"], cell["north"], inclusive="left") &
                    history["longitude"].between(cell["west"], cell["east"], inclusive="left")
                ]
                cell["hotspot_count"] = int(len(matches))
        else:
            for cell in cells:
                cell["hotspot_count"] = 0

        max_history = max((c["hotspot_count"] for c in cells), default=0)

        cache = weather_cache.get(step)
        if cache and time.time() - cache["time"] < 900:
            weather = cache["data"]
        else:
            weather = []
            for offset in range(0, len(cells), 50):
                batch = cells[offset:offset + 50]
                response = requests.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": ",".join(str(c["lat"]) for c in batch),
                        "longitude": ",".join(str(c["lon"]) for c in batch),
                        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                        "hourly": "precipitation",
                        "past_hours": 24,
                        "forecast_hours": 1,
                        "timezone": "auto"
                    },
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()
                weather.extend(result if isinstance(result, list) else [result])
            if len(weather) != len(cells):
                raise ValueError("Weather grid size mismatch.")
            weather_cache[step] = {"time": time.time(), "data": weather}

        for cell, item in zip(cells, weather):
            current = item["current"]
            rain_values = item["hourly"]["precipitation"][:-1]
            rain = sum(v for v in rain_values if v is not None)
            temp = current["temperature_2m"]
            humidity = current["relative_humidity_2m"]
            wind = current["wind_speed_10m"]
            if any(v is None for v in (temp, humidity, wind)):
                continue
            score = weather_score(temp, humidity, wind, rain)
            history_score = round(cell["hotspot_count"] / max_history * 100) if max_history else 0
            cell.update({
                "weather": {
                    "temperature": temp, "humidity": humidity,
                    "wind_speed": wind, "rain_last_24h": round(rain, 1)
                },
                "weather_score": score["score"],
                "weather_level": score["level"],
                "history_score": history_score,
                "combined_score": round(score["score"] * 0.7 + history_score * 0.3)
            })

        return jsonify({
            "cells": cells, "grid_size": step,
            "history_note": history_note,
            "history_period": f"{start} to {end}"
        })
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except requests.RequestException as error:
        app.logger.exception("Weather or FIRMS request failed")
        return jsonify({"error": "A data service is temporarily unavailable.", "details": str(error)}), 502
    except (KeyError, TypeError) as error:
        app.logger.exception("Risk grid calculation failed")
        return jsonify({"error": "Could not calculate the risk grid.", "details": str(error)}), 500


@app.route("/api/risk")
def weather_risk():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "hourly": "precipitation", "past_hours": 24,
                "forecast_hours": 1, "timezone": "auto"
            }, timeout=20
        )
        response.raise_for_status()
        data = response.json()
        current = data["current"]
        rain = sum(v for v in data["hourly"]["precipitation"][:-1] if v is not None)
        result = weather_score(current["temperature_2m"], current["relative_humidity_2m"], current["wind_speed_10m"], rain)
        return jsonify({"latitude": lat, "longitude": lon, "weather_score": result})
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "Invalid coordinates or weather response."}), 400
    except requests.RequestException:
        return jsonify({"error": "Weather service unavailable."}), 502


@app.route("/api/infrastructure")
def infrastructure():
    try:
        south = float(request.args.get("south", -56))
        west = float(request.args.get("west", -76))
        north = float(request.args.get("north", -38))
        east = float(request.args.get("east", -63))
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            return jsonify({"error": "Invalid map bounds."}), 400

        query = f"""
        [out:json][timeout:45];
        (
          nwr["amenity"="fire_station"]({south},{west},{north},{east});
          node["emergency"="fire_hydrant"]({south},{west},{north},{east});
          node["amenity"="drinking_water"]({south},{west},{north},{east});
          node["emergency"="fire_water_pond"]({south},{west},{north},{east});
        );
        out center;
        """
        response = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers={"User-Agent": "KutralApp/1.0", "Accept": "application/json"},
            timeout=60
        )
        response.raise_for_status()
        results = []
        for item in response.json().get("elements", []):
            tags = item.get("tags", {})
            pos = item if "lat" in item else item.get("center", {})
            if not pos:
                continue
            kind = (
                "fire_station" if tags.get("amenity") == "fire_station" else
                "fire_hydrant" if tags.get("emergency") == "fire_hydrant" else
                "drinking_water" if tags.get("amenity") == "drinking_water" else
                "fire_water_pond" if tags.get("emergency") == "fire_water_pond" else None
            )
            if kind:
                results.append({
                    "id": item["id"], "type": kind,
                    "name": tags.get("name", "Unnamed"),
                    "latitude": pos["lat"], "longitude": pos["lon"],
                    "operator": tags.get("operator"), "phone": tags.get("phone")
                })
        return jsonify({"elements": results, "count": len(results), "source": "OpenStreetMap"})
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid map bounds."}), 400
    except requests.RequestException as error:
        app.logger.exception("Overpass request failed")
        return jsonify({"error": "OpenStreetMap infrastructure service unavailable.", "details": str(error)}), 502


@app.route("/api/elevation")
def elevation():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        response = requests.get(
            "https://api.open-meteo.com/v1/elevation",
            params={"latitude": lat, "longitude": lon}, timeout=20
        )
        response.raise_for_status()
        return jsonify({"latitude": lat, "longitude": lon, "elevation_m": response.json()["elevation"][0]})
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "Invalid coordinates."}), 400
    except requests.RequestException:
        return jsonify({"error": "Elevation service unavailable."}), 502


@app.route("/api/route")
def route():
    try:
        a = (float(request.args["start_lat"]), float(request.args["start_lon"]))
        b = (float(request.args["end_lat"]), float(request.args["end_lon"]))
        for lat, lon in (a, b):
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        url = f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
        response = requests.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return jsonify({"error": "No mapped route found."}), 404
        result = data["routes"][0]
        return jsonify({
            "distance_km": round(result["distance"] / 1000, 2),
            "duration_minutes": round(result["duration"] / 60, 1),
            "geometry": result["geometry"]
        })
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "Invalid route coordinates."}), 400
    except requests.RequestException:
        return jsonify({"error": "Routing service unavailable."}), 502


@app.route("/api/population-cell")
def population_cell():
    try:
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        step = float(request.args.get("step", 1))
        if step not in (0.25, 0.5, 1, 2):
            return jsonify({"error": "Invalid cell size."}), 400
        s, n, w, e = lat - step / 2, lat + step / 2, lon - step / 2, lon + step / 2
        polygon = {"type": "Polygon", "coordinates": [[[w,s],[e,s],[e,n],[w,n],[w,s]]]}
        response = requests.post(
            "https://api.worldpop.org/v3/population",
            json={"geojson": polygon, "year": 2025}, timeout=60
        )
        response.raise_for_status()
        data = response.json()
        return jsonify({
            "latitude": lat, "longitude": lon,
            "population": data.get("total_population"),
            "density_per_km2": data.get("population_density"),
            "area_km2": data.get("area_km2"),
            "data_year": data.get("data_year"), "source": data.get("data_source")
        })
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid coordinates or cell size."}), 400
    except requests.RequestException:
        return jsonify({"error": "Population service unavailable."}), 502


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
