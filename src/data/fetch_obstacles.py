"""
fetch_obstacles.py
==================
Downloads and parses Swiss air navigation obstacle data from the Federal Office
of Civil Aviation (BAZL) via the geo.admin.ch STAC API.

Data source:
    - Collection: ch.bazl.luftfahrthindernis (obstacles >= 25m unbuilt / >= 60m built)
    - Collection: ch.bazl.luftfahrthindernis-klein (smaller airport obstacles)
    - Publisher: Bundesamt für Zivilluftfahrt (BAZL)
    - License: Open Government Data (OGD) — free use with source attribution
    - Attribution: © Federal Office of Civil Aviation BAZL, opendata.swiss

Output:
    - data/raw/luftfahrthindernis_4326.kmz       (original KMZ download)
    - data/raw/luftfahrthindernis_klein.csv       (smaller obstacles CSV)
    - data/processed/obstacles.parquet            (cleaned, merged GeoDataFrame)

Usage:
    python src/data/fetch_obstacles.py

Requirements:
    pip install requests fastkml shapely geopandas pandas pyproj lxml
"""

import io
import json
import logging
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # assumes src/data/fetch_obstacles.py
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

STAC_BASE = "https://data.geo.admin.ch/api/stac/v0.9"
COLLECTION_MAIN = "ch.bazl.luftfahrthindernis"
COLLECTION_SMALL = "ch.bazl.luftfahrthindernis-klein"
ITEM_MAIN = "luftfahrthindernis"

# Direct download fallback URLs (if STAC item structure changes)
KMZ_FALLBACK_URL = (
    "https://data.geo.admin.ch/ch.bazl.luftfahrthindernis/"
    "luftfahrthindernis/luftfahrthindernis_4326.kmz"
)

# KML namespace
KML_NS = "{http://www.opengis.net/kml/2.2}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def ensure_dirs():
    """Create output directories if they don't exist."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Output dirs ready: {RAW_DIR}, {PROCESSED_DIR}")


def download_file(url: str, dest: Path, description: str = "") -> Path:
    """Download a file with progress logging."""
    log.info(f"Downloading {description or url} ...")
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()

    total = int(response.headers.get("content-length", 0))
    downloaded = 0

    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)

    size_mb = downloaded / (1024 * 1024)
    log.info(f"Saved {dest.name} ({size_mb:.1f} MB)")
    return dest


# ---------------------------------------------------------------------------
# 1. Download main obstacles (KMZ)
# ---------------------------------------------------------------------------


def fetch_kmz_url_from_stac() -> str:
    """Query STAC API to find the KMZ asset download URL."""
    items_url = f"{STAC_BASE}/collections/{COLLECTION_MAIN}/items/{ITEM_MAIN}"
    log.info(f"Querying STAC: {items_url}")

    try:
        r = requests.get(items_url, timeout=30)
        r.raise_for_status()
        item = r.json()

        # Look for KMZ asset
        for key, asset in item.get("assets", {}).items():
            if key.endswith(".kmz") or asset.get("type", "").endswith("kmz"):
                log.info(f"Found KMZ asset: {key}")
                return asset["href"]

        # If no .kmz key found, list available assets for debugging
        available = list(item.get("assets", {}).keys())
        log.warning(f"No KMZ asset found. Available assets: {available}")

    except requests.RequestException as e:
        log.warning(f"STAC query failed: {e}")

    return ""


def download_main_obstacles() -> Path:
    """Download the main obstacle KMZ file."""
    dest = RAW_DIR / "luftfahrthindernis_4326.kmz"

    if dest.exists():
        log.info(f"KMZ already exists at {dest}, skipping download. Delete to re-fetch.")
        return dest

    # Try STAC API first
    kmz_url = fetch_kmz_url_from_stac()

    # Fallback to direct URL
    if not kmz_url:
        log.info("Using fallback download URL")
        kmz_url = KMZ_FALLBACK_URL

    return download_file(kmz_url, dest, "main obstacles KMZ")


# ---------------------------------------------------------------------------
# 2. Download small airport obstacles (CSV)
# ---------------------------------------------------------------------------


def fetch_small_obstacles_csv_url() -> str:
    """Query STAC API for the small obstacles CSV download URL."""
    items_url = f"{STAC_BASE}/collections/{COLLECTION_SMALL}/items"
    log.info(f"Querying STAC for small obstacles: {items_url}")

    try:
        r = requests.get(items_url, timeout=30)
        r.raise_for_status()
        data = r.json()

        for item in data.get("features", []):
            for key, asset in item.get("assets", {}).items():
                if ".csv" in key.lower() or "csv" in asset.get("type", "").lower():
                    log.info(f"Found CSV asset: {key}")
                    return asset["href"]

    except requests.RequestException as e:
        log.warning(f"STAC query for small obstacles failed: {e}")

    return ""


def download_small_obstacles() -> Path | None:
    """Download the small airport obstacles CSV. Returns None if unavailable."""
    dest = RAW_DIR / "luftfahrthindernis_klein.csv"

    if dest.exists():
        log.info(f"Small obstacles CSV already exists at {dest}, skipping.")
        return dest

    csv_url = fetch_small_obstacles_csv_url()
    if not csv_url:
        log.warning("Small obstacles CSV URL not found — skipping (non-critical).")
        return None

    return download_file(csv_url, dest, "small airport obstacles CSV")


# ---------------------------------------------------------------------------
# 3. Parse KMZ → GeoDataFrame
# ---------------------------------------------------------------------------


def extract_kml_from_kmz(kmz_path: Path) -> bytes:
    """Unzip KMZ and return the KML content as bytes."""
    with zipfile.ZipFile(kmz_path, "r") as z:
        kml_files = [f for f in z.namelist() if f.lower().endswith(".kml")]
        if not kml_files:
            raise ValueError(f"No .kml file found inside {kmz_path}")
        log.info(f"Extracting {kml_files[0]} from KMZ")
        return z.read(kml_files[0])


def parse_extended_data(placemark: ET.Element) -> dict:
    """Extract all key-value pairs from a Placemark's ExtendedData."""
    data = {}
    extended = placemark.find(f"{KML_NS}ExtendedData")
    if extended is None:
        return data

    # Pattern 1: <Data name="..."><value>...</value></Data>
    for d in extended.findall(f"{KML_NS}Data"):
        name = d.get("name", "")
        value_el = d.find(f"{KML_NS}value")
        if name and value_el is not None:
            data[name] = value_el.text

    # Pattern 2: <SchemaData><SimpleData name="...">...</SimpleData></SchemaData>
    for sd in extended.findall(f".//{KML_NS}SimpleData"):
        name = sd.get("name", "")
        if name:
            data[name] = sd.text

    return data


def parse_coordinates(placemark: ET.Element) -> tuple[float, float, float] | None:
    """Extract lon, lat, alt from a Placemark's Point geometry."""
    point = placemark.find(f".//{KML_NS}Point/{KML_NS}coordinates")
    if point is not None and point.text:
        parts = point.text.strip().split(",")
        lon = float(parts[0])
        lat = float(parts[1])
        alt = float(parts[2]) if len(parts) > 2 else 0.0
        return lon, lat, alt

    # Some obstacles may use other geometry types (LineString for cables)
    linestring = placemark.find(f".//{KML_NS}LineString/{KML_NS}coordinates")
    if linestring is not None and linestring.text:
        # Take midpoint of line
        coords = []
        for coord_str in linestring.text.strip().split():
            parts = coord_str.split(",")
            coords.append((float(parts[0]), float(parts[1])))
        if coords:
            mid_lon = sum(c[0] for c in coords) / len(coords)
            mid_lat = sum(c[1] for c in coords) / len(coords)
            return mid_lon, mid_lat, 0.0

    return None


def parse_kml_to_dataframe(kml_bytes: bytes) -> gpd.GeoDataFrame:
    """Parse KML bytes into a GeoDataFrame with all obstacle attributes."""
    log.info("Parsing KML (this may take a minute for large files)...")

    root = ET.fromstring(kml_bytes)
    placemarks = root.findall(f".//{KML_NS}Placemark")
    log.info(f"Found {len(placemarks)} Placemarks")

    records = []
    skipped = 0

    for pm in placemarks:
        coords = parse_coordinates(pm)
        if coords is None:
            skipped += 1
            continue

        lon, lat, alt = coords
        attrs = parse_extended_data(pm)

        # Add name/description if present
        name_el = pm.find(f"{KML_NS}name")
        desc_el = pm.find(f"{KML_NS}description")
        if name_el is not None and name_el.text:
            attrs["placemark_name"] = name_el.text
        if desc_el is not None and desc_el.text:
            attrs["placemark_description"] = desc_el.text

        attrs["longitude"] = lon
        attrs["latitude"] = lat
        attrs["kml_altitude"] = alt

        records.append(attrs)

    if skipped > 0:
        log.warning(f"Skipped {skipped} Placemarks without parseable coordinates")

    log.info(f"Parsed {len(records)} obstacle records")

    df = pd.DataFrame(records)

    # Convert to GeoDataFrame with WGS84 CRS
    geometry = [Point(row["longitude"], row["latitude"]) for _, row in df.iterrows()]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")

    return gdf


# ---------------------------------------------------------------------------
# 4. Clean and merge
# ---------------------------------------------------------------------------


def clean_main_obstacles(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Basic cleaning of the main obstacle GeoDataFrame."""
    log.info(f"Cleaning main obstacles: {len(gdf)} rows, {len(gdf.columns)} columns")
    log.info(f"Columns: {list(gdf.columns)}")

    # Convert numeric columns where possible
    numeric_candidates = ["maxheightagl", "maxheight", "elevation", "radius",
                          "totalheight", "height_agl", "heightagl"]
    for col in numeric_candidates:
        if col in gdf.columns:
            gdf[col] = pd.to_numeric(gdf[col], errors="coerce")

    # Drop exact duplicates
    n_before = len(gdf)
    gdf = gdf.drop_duplicates()
    n_dropped = n_before - len(gdf)
    if n_dropped > 0:
        log.info(f"Dropped {n_dropped} duplicate rows")

    # Add source tag
    gdf["source"] = "main"

    return gdf


def load_small_obstacles(csv_path: Path | None) -> gpd.GeoDataFrame | None:
    """Load and convert the small airport obstacles CSV to a GeoDataFrame."""
    if csv_path is None or not csv_path.exists():
        return None

    log.info(f"Loading small obstacles from {csv_path}")
    df = pd.read_csv(csv_path)
    log.info(f"Small obstacles: {len(df)} rows, columns: {list(df.columns)}")

    # Try common column names for coordinates
    lon_col = next((c for c in df.columns if c.lower() in
                    ["longitude", "lon", "lng", "x", "wgs84_e"]), None)
    lat_col = next((c for c in df.columns if c.lower() in
                    ["latitude", "lat", "y", "wgs84_n"]), None)

    if lon_col is None or lat_col is None:
        log.warning(f"Cannot identify coordinate columns in CSV. "
                    f"Available: {list(df.columns)}")
        log.warning("Saving CSV as-is. Manual inspection needed in Notebook 01.")
        return None

    geometry = [Point(row[lon_col], row[lat_col]) for _, row in df.iterrows()]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    gdf["source"] = "small_airport"

    return gdf


# ---------------------------------------------------------------------------
# 5. Main pipeline
# ---------------------------------------------------------------------------


def main():
    log.info("=" * 60)
    log.info("Swiss Air Navigation Obstacle Data — Fetch Pipeline")
    log.info("=" * 60)

    ensure_dirs()

    # Step 1: Download
    kmz_path = download_main_obstacles()
    small_csv_path = download_small_obstacles()

    # Step 2: Parse KMZ
    kml_bytes = extract_kml_from_kmz(kmz_path)
    gdf_main = parse_kml_to_dataframe(kml_bytes)
    gdf_main = clean_main_obstacles(gdf_main)

    # Step 3: Load small obstacles
    gdf_small = load_small_obstacles(small_csv_path)

    # Step 4: Merge if both available
    if gdf_small is not None:
        # Align columns before concat (outer join keeps all columns)
        gdf = pd.concat([gdf_main, gdf_small], ignore_index=True)
        log.info(f"Merged dataset: {len(gdf)} obstacles "
                 f"({len(gdf_main)} main + {len(gdf_small)} small airport)")
    else:
        gdf = gdf_main
        log.info(f"Dataset: {len(gdf)} obstacles (main only)")

    # Step 5: Save
    out_path = PROCESSED_DIR / "obstacles.parquet"
    gdf.to_parquet(out_path, index=False)
    log.info(f"Saved to {out_path}")

    # Summary
    log.info("")
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info(f"Total obstacles: {len(gdf)}")
    log.info(f"Columns: {list(gdf.columns)}")
    log.info(f"CRS: {gdf.crs}")
    log.info(f"Bounds: {gdf.total_bounds}")
    if "source" in gdf.columns:
        log.info(f"By source:\n{gdf['source'].value_counts().to_string()}")
    log.info(f"Output: {out_path}")
    log.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"Pipeline failed: {e}", exc_info=True)
        sys.exit(1)
