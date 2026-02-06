import sys
import json
import time
import urllib.request
import urllib.error

# --- Configuration ---
LOCATION_API = "http://ip-api.com/json/"

# Updated API: Added humidity, daily highs/lows, and precip probability. 
# Added timezone=auto so 'daily' refers to local time.
WEATHER_API_TEMPLATE = (
    "https://api.open-meteo.com/v1/forecast?"
    "latitude={}&longitude={}&"
    "current=temperature_2m,weather_code,is_day,wind_speed_10m,relative_humidity_2m&"
    "daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,snowfall_sum&"
    "temperature_unit=celsius&"
    "timezone=auto&"
    "forecast_days=1"
)

def dlog(*args):
    print("[weather_bridge]", *args, file=sys.stderr, flush=True)

def get_location():
    try:
        with urllib.request.urlopen(LOCATION_API, timeout=5) as url:
            data = json.loads(url.read().decode())
            if data.get("status") == "success":
                return data["lat"], data["lon"], data["city"]
    except Exception as e:
        dlog("Location fetch failed:", e)
    return None, None, "Unknown"

def get_weather(lat, lon):
    try:
        url = WEATHER_API_TEMPLATE.format(lat, lon)
        with urllib.request.urlopen(url, timeout=5) as req:
            data = json.loads(req.read().decode())
            return data
    except Exception as e:
        dlog("Weather fetch failed:", e)
    return None

def decode_wmo(code):
    if code == 0: return "Clear Sky"
    if code == 1: return "Mainly Clear"
    if code == 2: return "Partly Cloudy"
    if code == 3: return "Overcast"
    if code in [45, 48]: return "Foggy"
    if code in [51, 53, 55]: return "Drizzle"
    if code in [56, 57]: return "Freezing Drizzle"
    if code in [61, 63, 65]: return "Rain"
    if code in [66, 67]: return "Freezing Rain"
    if code in [71, 73, 75]: return "Snow Fall"
    if code == 77: return "Snow Grains"
    if code in [80, 81, 82]: return "Rain Showers"
    if code in [85, 86]: return "Snow Showers"
    if code == 95: return "Thunderstorm"
    if code in [96, 99]: return "Thunderstorm & Hail"
    return "Unknown"

def main():
    lat, lon, city = get_location()
    
    while lat is None:
        time.sleep(10)
        lat, lon, city = get_location()

    dlog(f"Locked on: {city} ({lat}, {lon})")
    last_sent = ""
    
    while True:
        try:
            weather_data = get_weather(lat, lon)
            
            if weather_data and 'current' in weather_data:
                cur = weather_data['current']
                daily = weather_data.get('daily', {})

                # Current Data
                temp = cur.get('temperature_2m', 0)
                is_day = cur.get('is_day', 1) == 1
                wmo_code = cur.get('weather_code', 0)
                wind = cur.get('wind_speed_10m', 0)
                humidity = cur.get('relative_humidity_2m', 0)

                # Daily Data (Arrays)
                high = daily.get('temperature_2m_max', [0])[0]
                low = daily.get('temperature_2m_min', [0])[0]
                precip_prob = daily.get('precipitation_probability_max', [0])[0]
                snowfall = daily.get('snowfall_sum', [0.0])[0]
                
                condition = decode_wmo(wmo_code)

                payload = {
                    "temp": round(temp),
                    "condition": condition,
                    "isDay": is_day,
                    "city": city,
                    "wmo": wmo_code,
                    "wind": round(wind),
                    "humidity": round(humidity),
                    "high": round(high),
                    "low": round(low),
                    "precip_prob": precip_prob,
                    "snowfall": snowfall
                }
                
                json_str = json.dumps(payload)
                if json_str != last_sent:
                    print(f"<<WEATHER>>{json_str}<<END>>", flush=True)
                    last_sent = json_str
            
            time.sleep(900) # 15 mins

        except Exception as e:
            dlog("Loop Error:", e)
            time.sleep(60)

if __name__ == "__main__":
    main()