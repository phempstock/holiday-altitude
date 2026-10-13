# Holiday Altitude

A free web map that shades the land between two altitudes you choose, to help pick holiday accommodation that avoids lowland heat and mountain cold. It opens on Costa Rica, and works anywhere in the world.

## Features
- Shade by **altitude**: lowest and highest altitude sliders (or type exact figures); the shading updates as you move them
- Shade by **temperature** (Costa Rica, Spain, Portugal, Balearic and Canary Islands for now): pick the month of your visit and the range of typical daytime highs you want, plus the coldest nights you'll accept; optional +1 °C for recent warming
- Click anywhere to see its height, whether it's inside your band, and its typical highs and lows through the year
- Search for countries, towns and small villages
- Light hill shading; fold-away panel on phones
- Installable on phones from the browser's "Add to Home Screen" / "Install app"

## Versions
- `restore/v1.0-altitude` branch: the altitude-only version, kept unchanged as a restore point.
- `climate` branch: where temperature features are developed before going live on `main`.

## Data and credits (all free, no API keys)
- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright)
- Map tiles: [OpenFreeMap](https://openfreemap.org)
- Elevation: [Terrain Tiles on AWS](https://registry.opendata.aws/terrain-tiles/) ([sources](https://github.com/tilezen/joerd/blob/master/docs/attribution.md))
- Temperatures: [CHELSA](https://www.chelsa-climate.org) V2.1 climatologies, 1981–2010 (Karger et al., CC0), converted to map tiles by `scripts/build_climate_tiles.py` via the GitHub Actions job in `.github/workflows/build-climate.yml`
- Place search: [Nominatim](https://nominatim.org) (fair-use limit of one search per second)
- Map library: [MapLibre GL JS](https://maplibre.org)

Heights come from global data with about 30 m detail, so individual readings can differ from local signs by a few tens of metres.

## Running it
It's a static site: open `index.html` in a browser, or host the folder on GitHub Pages. Installing on a phone and the offline app shell only work when served over https.
