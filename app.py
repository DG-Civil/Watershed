import base64
import gc
import io
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import json

import branca.colormap as cmp
import folium
import geopandas as gpd
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
import rasterio
import rasterio.features
import requests
import shapely.geometry as sg
import shapely.ops
import streamlit as st
import whitebox
from matplotlib import cm
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import Affine, array_bounds
from rasterio.warp import calculate_default_transform, reproject
from rasterio.windows import Window
from scipy.interpolate import griddata
from shapely.geometry import box
from streamlit_folium import st_folium
import matplotlib as mpl

import psutil


st.set_page_config(
    page_title="Hydrology & CN Web Suite",
    page_icon="🌊",
    layout="wide",
)

US_STATES = [
    "Alabama",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "District of Columbia",
    "Florida",
    "Georgia",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
]

# -----------------------------------------------------------------------------
# HELPER & IN-MEMORY RASTER / REST FUNCTIONS
# -----------------------------------------------------------------------------

import ctypes


def enforce_cloud_memory_limit(limit_mb=900):
  # 1. Force Python garbage collection
  gc.collect()

  # 2. Force Linux to release C-extension memory arenas back to the OS
  try:
    libc = ctypes.CDLL("libc.so.6")
    libc.malloc_trim(0)
  except Exception:
    pass

  parent = psutil.Process(os.getpid())
  processes = [parent] + parent.children(recursive=True)

  total_bytes = 0
  process_info = []

  for proc in processes:
    try:
      mem_bytes = proc.memory_info().rss
      total_bytes += mem_bytes
      process_info.append({
          "role": "Parent" if proc.pid == parent.pid else "Child",
          "name": proc.name(),
          "pid": proc.pid,
          "mem_mb": mem_bytes / (1024 * 1024),
      })
    except (psutil.NoSuchProcess, psutil.AccessDenied):
      continue

  total_mb = total_bytes / (1024 * 1024)

  if total_mb > limit_mb:
    breakdown_md = "\n".join(
        f"* **{p['role']} Process** (`{p['name']}` | PID `{p['pid']}`):"
        f" **{p['mem_mb']:.2f} MB**"
        for p in process_info
    )

    st.error(
        f"⚠️ **Memory Limit Exceeded ({total_mb:.1f} MB / {limit_mb} MB)**\n\n"
        f"Execution stopped to prevent a container crash. Active process"
        f" usage:\n\n{breakdown_md}"
    )

    st.warning(
        "Click the button below to purge active session memory and safely"
        " restart the application state."
    )

    # Definining a safe callback keeps Streamlit from breaking on initial boot
    def purge_callback():
      st.session_state.clear()
      st.cache_data.clear()
      st.cache_resource.clear()
      gc.collect()
      try:
        libc.malloc_trim(0)
      except Exception:
        pass

    # Using on_click ensures this logic ONLY fires when a real user clicks it
    st.button(
        "🔄 Purge Memory & Reload Session",
        type="primary",
        on_click=purge_callback,
    )

    st.stop()


RAM_limit = 1100


# def enforce_cloud_memory_limit(limit_mb=900):
#   # 1. Force Python garbage collection
#   gc.collect()

#   # 2. Force Linux to release C-extension memory arenas back to the OS
#   try:
#     libc = ctypes.CDLL("libc.so.6")
#     libc.malloc_trim(0)
#   except Exception:
#     pass

#   parent = psutil.Process(os.getpid())
#   processes = [parent] + parent.children(recursive=True)

#   total_bytes = 0
#   process_info = []

#   for proc in processes:
#     try:
#       mem_bytes = proc.memory_info().rss
#       total_bytes += mem_bytes
#       process_info.append({
#           "role": "Parent" if proc.pid == parent.pid else "Child",
#           "name": proc.name(),
#           "pid": proc.pid,
#           "mem_mb": mem_bytes / (1024 * 1024),
#       })
#     except (psutil.NoSuchProcess, psutil.AccessDenied):
#       continue

#   total_mb = total_bytes / (1024 * 1024)

#   if total_mb > limit_mb:
#     breakdown_md = "\n".join(
#         f"* **{p['role']} Process** (`{p['name']}` | PID `{p['pid']}`):"
#         f" **{p['mem_mb']:.2f} MB**"
#         for p in process_info
#     )

#     st.error(
#         f"⚠️ **Memory Limit Exceeded ({total_mb:.1f} MB / {limit_mb} MB)**\n\n"
#         f"Execution stopped to prevent a container crash. Active process"
#         f" usage:\n\n{breakdown_md}"
#     )

#     st.warning(
#         "Click the button below to purge active session memory and safely"
#         " restart the application state."
#     )

#     # 3. Soft reset: Purge memory references and reload Streamlit state
#     if st.button("🔄 Purge Memory & Reload Session", type="primary"):
#       st.session_state.clear()
#       st.cache_data.clear()
#       st.cache_resource.clear()

#       gc.collect()
#       try:
#         libc.malloc_trim(0)
#       except Exception:
#         pass

#       st.rerun()

#     st.stop()

# RAM_limit=1100


# def enforce_cloud_memory_limit(limit_mb=900):
#     # 1. Force Python garbage collection
#     gc.collect()
    
#     # 2. Force Linux to release C-extension memory arenas back to the OS
#     try:
#         libc = ctypes.CDLL("libc.so.6")
#         libc.malloc_trim(0)
#     except Exception:
#         pass

#     parent = psutil.Process(os.getpid())
#     processes = [parent] + parent.children(recursive=True)
    
#     total_bytes = 0
#     process_info = []

#     for proc in processes:
#         try:
#             mem_bytes = proc.memory_info().rss
#             total_bytes += mem_bytes
#             process_info.append({
#                 "role": "Parent" if proc.pid == parent.pid else "Child",
#                 "name": proc.name(),
#                 "pid": proc.pid,
#                 "mem_mb": mem_bytes / (1024 * 1024)
#             })
#         except (psutil.NoSuchProcess, psutil.AccessDenied):
#             continue

#     total_mb = total_bytes / (1024 * 1024)

#     if total_mb > limit_mb:
#         # 3. Purge Streamlit session and caches to drop heavy references
#         for key in list(st.session_state.keys()):
#             del st.session_state[key]
            
#         st.cache_data.clear()
#         st.cache_resource.clear()

#         # 4. Run GC and malloc_trim one more time after purging Streamlit data
#         gc.collect()
#         try:
#             libc.malloc_trim(0)
#         except Exception:
#             pass

#         breakdown_md = "\n".join(
#             f"* **{p['role']} Process** (`{p['name']}` | PID `{p['pid']}`): **{p['mem_mb']:.2f} MB**"
#             for p in process_info
#         )
        
#         st.error(
#             f"⚠️ **Memory Limit Exceeded ({total_mb:.1f} MB / {limit_mb} MB)**\n\n"
#             f"Execution stopped to prevent a container crash. Active process usage:\n\n"
#             f"{breakdown_md}\n\n"
#             f"🧹 **Session memory and caches have been purged.**"
#         )
        
#         st.warning("If a page refresh still results in this error, the memory is locked by the system. Click the button below to restart the container and clear the RAM entirely.")
        
#         # 5. Provide a hard-reset escape hatch that guarantees 0 RAM usage
#         if st.button("🔄 Hard Reset Server Memory", type="primary"):
#             os._exit(0)
            
#         st.stop()






# def enforce_cloud_memory_limit(limit_mb=900):
#     """
#     Monitors Streamlit process RAM. If limit is exceeded, purges session state,
#     clears Streamlit caches, forces garbage collection, and stops execution.
#     """
#     # 1. Force GC sweep first to clear lingering unreferenced objects
#     gc.collect()

#     parent = psutil.Process(os.getpid())
#     processes = [parent] + parent.children(recursive=True)
    
#     total_bytes = 0
#     process_info = []

#     for proc in processes:
#         try:
#             mem_bytes = proc.memory_info().rss
#             total_bytes += mem_bytes
#             process_info.append({
#                 "role": "Parent" if proc.pid == parent.pid else "Child",
#                 "name": proc.name(),
#                 "pid": proc.pid,
#                 "mem_mb": mem_bytes / (1024 * 1024)
#             })
#         except (psutil.NoSuchProcess, psutil.AccessDenied):
#             continue

#     total_mb = total_bytes / (1024 * 1024)

#     if total_mb > limit_mb:
#         # 2. Clear all session state keys to release heavy objects/buffers
#         for key in list(st.session_state.keys()):
#             del st.session_state[key]

#         # 3. Clear Streamlit internal function caches
#         st.cache_data.clear()
#         st.cache_resource.clear()

#         # 4. Force aggressive Garbage Collection
#         gc.collect()

#         breakdown_md = "\n".join(
#             f"* **{p['role']} Process** (`{p['name']}` | PID `{p['pid']}`): **{p['mem_mb']:.2f} MB**"
#             for p in process_info
#         )
        
#         st.error(
#             f"⚠️ **Memory Limit Exceeded ({total_mb:.1f} MB / {limit_mb} MB)**\n\n"
#             f"Execution stopped to prevent a container crash. Active process usage:\n\n"
#             f"{breakdown_md}\n\n"
#             f"🧹 **Session memory and caches have been purged.** Please refresh the page and try uploading smaller/fewer files."
#         )
#         st.stop()




# def enforce_cloud_memory_limit(limit_mb=900):
#     """
#     Monitors Streamlit RAM usage and displays a detailed process breakdown 
#     (parent + children) if memory consumption crosses the safety limit.
#     """
#     parent = psutil.Process(os.getpid())
#     processes = [parent] + parent.children(recursive=True)
    
#     process_info = []
#     total_bytes = 0

#     for proc in processes:
#         try:
#             mem_bytes = proc.memory_info().rss
#             total_bytes += mem_bytes
#             process_info.append({
#                 "role": "Parent" if proc.pid == parent.pid else "Child",
#                 "name": proc.name(),
#                 "pid": proc.pid,
#                 "mem_mb": mem_bytes / (1024 * 1024)
#             })
#         except (psutil.NoSuchProcess, psutil.AccessDenied):
#             # Handles edge cases where a subprocess terminates mid-check
#             continue

#     total_mb = total_bytes / (1024 * 1024)

#     if total_mb > limit_mb:
#         # Construct breakdown list for the Streamlit UI
#         breakdown_md = "\n".join(
#             f"* **{p['role']} Process** (`{p['name']}` | PID `{p['pid']}`): **{p['mem_mb']:.2f} MB**"
#             for p in process_info
#         )
        
#         st.error(
#             f"⚠️ **Memory Limit Exceeded ({total_mb:.1f} MB / {limit_mb} MB)**\n\n"
#             f"Execution stopped to prevent a container OOM reboot. Active process consumption:\n\n"
#             f"{breakdown_md}\n\n"
#             f"Please refresh the app and use smaller datasets or reduce concurrent operations."
#         )
#         st.stop()

# def enforce_cloud_memory_limit(limit_mb=900):
#     """
#     Monitors process RAM on Streamlit Community Cloud.
#     Stops execution before reaching the ~1 GB container OOM threshold.
#     """
#     parent = psutil.Process(os.getpid())
    
#     # Calculate memory of main Streamlit process + any spawned sub-processes
#     total_bytes = parent.memory_info().rss + sum(
#         child.memory_info().rss for child in parent.children(recursive=True)
#     )
#     mem_mb = total_bytes / (1024 * 1024)

#     if mem_mb > limit_mb:
#         st.error(
#             f"⚠️ **Memory Limit Reached ({mem_mb:.1f} MB / {limit_mb} MB):** "
#             "To prevent the server from crashing, execution was stopped. "
#             "Please clear your inputs or upload a smaller file."
#         )
#         st.stop()

# RAM_limit=1100

#@st.cache_data(ttl=120,  show_spinner=False)
def parse_landxml_to_geotiff(xml_input, output_tif_path, res=2.0):
    """Parses LandXML from path or buffer and writes geotiff to a scoped path."""
    tree = ET.parse(xml_input)
    root = tree.getroot()
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    crs_val = None
    coord_sys_elem = root.find(".//CoordinateSystem")
    if coord_sys_elem is not None and "desc" in coord_sys_elem.attrib:
        crs_val = coord_sys_elem.attrib["desc"]

    pnts = {}
    for p in root.findall(".//Pnts/P"):
        p_id = int(p.attrib["id"])
        coords = [float(x) for x in p.text.strip().split()]
        pnts[p_id] = (coords[1], coords[0], coords[2])

    if not pnts:
        raise ValueError("No valid points found in LandXML file.")

    points = np.array([(v[0], v[1]) for v in pnts.values()])
    values = np.array([v[2] for v in pnts.values()])
    min_x, min_y = points.min(axis=0)
    max_x, max_y = points.max(axis=0)

    grid_x, grid_y = np.mgrid[min_x:max_x:res, min_y:max_y:res]
    grid_z = griddata(points, values, (grid_x, grid_y), method="linear")
    grid_z = np.flipud(grid_z.T)

    height, width = grid_z.shape
    transform = rasterio.transform.from_bounds(
        min_x, min_y, max_x, max_y, width, height
    )
    assigned_crs = (
        crs_val
        if crs_val
        else f"+proj=tmerc +lat_0={(min_y+max_y)/2} +lon_0={(min_x+max_x)/2} +k=1.0 +x_0=0 +y_0=0 +units=m +no_defs"
    )

    with rasterio.open(
        output_tif_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=grid_z.dtype,
        crs=assigned_crs,
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(np.nan_to_num(grid_z, nan=-9999.0), 1)

    return output_tif_path

#@st.cache_data(ttl=120,  show_spinner=False)
def fetch_texas_streams_rest(target_crs, native_bounds):
    """
    Queries TxGIO ArcGIS REST service (NHD_TX_Rivers_Streams, Layer 1) 
    for stream vectors intersecting the DEM native bounds.
    """
    min_x, min_y, max_x, max_y = native_bounds
    
    # 1. Transform all 4 corners of the native DEM extent to EPSG:4326 (WGS84)
    transformer = Transformer.from_crs(target_crs, "EPSG:4326", always_xy=True)
    corners = [
        (min_x, min_y),
        (min_x, max_y),
        (max_x, min_y),
        (max_x, max_y),
    ]
    lons, lats = zip(*[transformer.transform(x, y) for x, y in corners])
    
    xmin, xmax = min(lons), max(lons)
    ymin, ymax = min(lats), max(lats)

    # 2. Query Layer 1 (Streams) instead of Layer 0 (Waterbodies) using EPSG:4326
    url = (
        "https://feature.geographic.texas.gov/arcgis/rest/services/"
        "Hydrography/Tx_Rivers_Streams_Waterbodies/MapServer/1/query"
    )
    params = {
        "where": "1=1",
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
        "outSR": "4326",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    
    response = requests.get(url, params=params, headers=headers, timeout=60)
    response.raise_for_status()
    geojson_data = response.json()

    if not geojson_data.get("features"):
        return None

    gdf = gpd.GeoDataFrame.from_features(geojson_data, crs="EPSG:4326")
    if gdf.empty:
        return None

    return gdf.to_crs(target_crs)

@st.cache_data(ttl=120,  show_spinner=False)
def extract_uploaded_archive_in_temp(uploaded_file, extract_to):
    """Extracts uploaded zip archive into target temp directory."""
    zip_bytes = io.BytesIO(uploaded_file.getbuffer())
    with zipfile.ZipFile(zip_bytes, "r") as zip_ref:
        zip_ref.extractall(extract_to)
    for root, _, files in os.walk(extract_to):
        for file in files:
            if file.endswith(".shp"):
                return os.path.join(root, file)
    return None

@st.cache_data(ttl=120,  show_spinner=False)
def get_wbt():
    import stat
    import os
    import requests
    import zipfile
    import whitebox
    
    # 1. Monkey-patch the download function to prevent writing to read-only site-packages
    whitebox.whitebox_tools.download_wbt = lambda *args, **kwargs: None
    
    # 2. Define the writable target directory in Streamlit Cloud
    wbt_dir = "/tmp/wbt_env"
    
    # UPDATE: The zip extracts into a parent directory named 'WhiteboxTools_linux_amd64'
    wbt_bin_dir = os.path.join(wbt_dir, "WhiteboxTools_linux_amd64", "WBT")
    exe_path = os.path.join(wbt_bin_dir, "whitebox_tools")
    
    # 3. Download and extract manually if it doesn't already exist
    if not os.path.exists(exe_path):
        os.makedirs(wbt_dir, exist_ok=True)
        url = "https://www.whiteboxgeo.com/WBT_Linux/WhiteboxTools_linux_amd64.zip"
        zip_path = os.path.join(wbt_dir, "wbt.zip")
        
        response = requests.get(url, timeout=120)
        with open(zip_path, "wb") as f:
            f.write(response.content)
            
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(wbt_dir)
            
        # Grant execution permissions to the binary
        os.chmod(exe_path, os.stat(exe_path).st_mode | stat.S_IEXEC)
        
    # 4. Instantiate and override the working directory
    wbt = whitebox.WhiteboxTools()
    wbt.set_whitebox_dir(wbt_bin_dir)
    return wbt

# -----------------------------------------------------------------------------
# APP INTERFACE
# -----------------------------------------------------------------------------

st.markdown(
    """
    <div style="text-align: center;">
        <h1 style="margin-bottom: 4px;">🌧️⛰️ Hydrology & Curve Number Calculator</h1>
        <p style="font-size: 18px; font-weight: bold; font-style: italic; color: #1a1a1a; margin-top: 0px; margin-bottom: 25px;">
            Developed by Dawit Ghebreyesus
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)


tab1, tab2 = st.tabs(
    [
        "⛰️ Watershed Delineation & Flow Analysis",
        "📊 Automated SSURGO & NLCD CN Generator",
    ]
)

with tab1:
    st.header("Watershed Delineation (WhiteboxTools Powered)")
    st.markdown(
        "[💡 If you want to download DEM over the state of Texas from TxGIO data hub](https://tnris-downloader.streamlit.app/)"
    )
    
    st.markdown("### Processing Mode")
    exec_mode = st.radio(
        "Select Processing Environment:",
        [
            "Cloud-Based (Max 150MB DEM, ~1GB RAM Limit)",
            "Local Windows Batch (No File Size or RAM Limits)",
        ],
        help="Use Local Windows Batch for high-resolution processing on your own machine without installing software.",
    )
    st.markdown("---")


    col_in, col_map = st.columns([1, 2])

    with col_in:
        st.subheader("1. Elevation & Hydro Inputs")
        terrain_files = st.file_uploader(
            "Upload DEM (GeoTIFF or LandXML)",
            type=["tif", "tiff", "xml"],
            accept_multiple_files=True,
        )

        st.markdown("**DEM Stream Burning (Optional)**")
        stream_option = st.radio(
            "Select Stream Vector Source:",
            [
                "None",
                "Upload Stream Shapefile (ZIP)",
                "Use Texas Gov Dataset (ArcGIS REST)",
            ],
            index=0,
            help="Burn streams into DEM prior to flow accumulation processing.",
        )

        stream_burn_file = None
        if stream_option == "Upload Stream Shapefile (ZIP)":
            stream_burn_file = st.file_uploader(
                "Upload Streams (ZIP Shapefile)",
                type=["zip"],
            )
        elif stream_option == "Use Texas Gov Dataset (ArcGIS REST)":
            st.info(
                "ℹ️ **Note:** The Texas hydrography dataset only applies to areas within the State of Texas."
            )

        threshold = st.number_input(
            "Flow Accumulation Threshold", min_value=1, value=1000, step=50
        )
        snap_dist = st.number_input(
            "Pour Point Snap Distance (m or ft)",
            min_value=1.0,
            value=100.0,
            step=10.0,
        )

        if terrain_files:
            total_size_mb = sum(f.size for f in terrain_files) / (1024 * 1024)
            if total_size_mb > 150:
                st.error(
                    f"❌ Total combined file size ({total_size_mb:.1f} MB) exceeds the 150 MB limit."
                )
                st.stop()

            is_multi = len(terrain_files) > 1
            has_xml = any(
                f.name.lower().endswith(".xml") for f in terrain_files
            )

            if is_multi and has_xml:
                st.error(
                    "❌ Multiple file uploads are only supported for GeoTIFF (.tif, .tiff) files. Please upload a single LandXML file or GeoTIFF files."
                )
                st.stop()

            current_filenames = [f.name for f in terrain_files]
            if st.session_state.get("last_filenames") != current_filenames:
                st.session_state["dem_files_data"] = [
                    {
                        "name": f.name,
                        "bytes": f.getvalue(),
                        "is_xml": f.name.lower().endswith(".xml"),
                    }
                    for f in terrain_files
                ]
                st.session_state["last_filenames"] = current_filenames

                processed_dems = []
                global_min = float("inf")
                global_max = float("-inf")
                all_map_bounds = []

                primary_crs = None
                native_min_x, native_min_y = float("inf"), float("inf")
                native_max_x, native_max_y = float("-inf"), float("-inf")

                with st.spinner(
                    "Processing elevation surfaces and map overlay..."
                ):
                    with tempfile.TemporaryDirectory() as preview_dir:
                        for dem_item in st.session_state["dem_files_data"]:
                            dem_tif_path = os.path.join(
                                preview_dir, f"prep_{dem_item['name']}.tif"
                            )
                            if dem_item["is_xml"]:
                                xml_buffer = io.BytesIO(dem_item["bytes"])
                                parse_landxml_to_geotiff(
                                    xml_buffer, dem_tif_path
                                )
                            else:
                                with open(dem_tif_path, "wb") as f:
                                    f.write(dem_item["bytes"])

                            with rasterio.open(dem_tif_path) as src:
                                if primary_crs is None:
                                    primary_crs = (
                                        src.crs
                                        if src.crs
                                        else f"+proj=tmerc +lat_0={(src.bounds.bottom + src.bounds.top)/2} +lon_0={(src.bounds.left + src.bounds.right)/2} +k=1.0 +x_0=0 +y_0=0 +units=m +no_defs"
                                    )

                                native_min_x = min(
                                    native_min_x, src.bounds.left
                                )
                                native_max_x = max(
                                    native_max_x, src.bounds.right
                                )
                                native_min_y = min(
                                    native_min_y, src.bounds.bottom
                                )
                                native_max_y = max(
                                    native_max_y, src.bounds.top
                                )

                                dst_crs = "EPSG:4326"
                                transform, width, height = (
                                    calculate_default_transform(
                                        src.crs,
                                        dst_crs,
                                        src.width,
                                        src.height,
                                        *src.bounds,
                                    )
                                )

                                max_dim = 500.0
                                ds_factor = int(
                                    max(1, max(width, height) / max_dim)
                                )
                                dst_width = max(1, width // ds_factor)
                                dst_height = max(1, height // ds_factor)
                                scaled_transform = transform * Affine.scale(
                                    ds_factor
                                )

                                destination = np.zeros(
                                    (dst_height, dst_width), dtype=np.float32
                                )
                                
                                enforce_cloud_memory_limit(RAM_limit)

                                reproject(
                                    source=rasterio.band(src, 1),
                                    destination=destination,
                                    src_transform=src.transform,
                                    src_crs=src.crs,
                                    dst_transform=scaled_transform,
                                    dst_crs=dst_crs,
                                    resampling=Resampling.average,
                                )
                                
                                enforce_cloud_memory_limit(RAM_limit)

                                valid_mask = ~np.isnan(destination)
                                if src.nodata is not None:
                                    valid_mask &= destination != src.nodata

                                if valid_mask.any():
                                    local_min = np.nanmin(
                                        destination[valid_mask]
                                    )
                                    local_max = np.nanmax(
                                        destination[valid_mask]
                                    )
                                    global_min = min(global_min, local_min)
                                    global_max = max(global_max, local_max)

                                (
                                    lon_min,
                                    lat_min,
                                    lon_max,
                                    lat_max,
                                ) = array_bounds(
                                    dst_height, dst_width, scaled_transform
                                )
                                bounds = [
                                    [lat_min, lon_min],
                                    [lat_max, lon_max],
                                ]
                                all_map_bounds.extend(bounds)

                                processed_dems.append(
                                    {
                                        "name": dem_item["name"],
                                        "array": destination,
                                        "mask": valid_mask,
                                        "bounds": bounds,
                                    }
                                )
                                del destination, valid_mask
                                gc.collect()

                st.session_state["processed_dems"] = processed_dems
                st.session_state["global_min"] = global_min
                st.session_state["global_max"] = global_max

                flat_lats = [b[0] for b in all_map_bounds]
                flat_lons = [b[1] for b in all_map_bounds]
                united_bounds = [
                    [min(flat_lats), min(flat_lons)],
                    [max(flat_lats), max(flat_lons)],
                ]

                st.session_state["map_bounds"] = united_bounds
                st.session_state["zoom_bounds"] = united_bounds
                st.session_state["target_crs"] = primary_crs
                st.session_state["native_bounds"] = (
                    native_min_x,
                    native_min_y,
                    native_max_x,
                    native_max_y,
                )
                st.session_state["outlet_x"] = (
                    native_min_x + native_max_x
                ) / 2.0
                st.session_state["outlet_y"] = (
                    native_min_y + native_max_y
                ) / 2.0
                st.session_state.pop("stream_gdf", None)
                st.session_state.pop("downslope_gdf", None)
                st.session_state.pop("watershed_results", None)

        if stream_option == "Upload Stream Shapefile (ZIP)" and stream_burn_file is not None:
            st.session_state["burn_streams_bytes"] = stream_burn_file.getbuffer()
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    zip_bytes = io.BytesIO(stream_burn_file.getvalue())
                    with zipfile.ZipFile(zip_bytes, "r") as z:
                        z.extractall(temp_dir)

                    shp_path = None
                    for root, _, files in os.walk(temp_dir):
                        for file in files:
                            if file.lower().endswith(".shp"):
                                shp_path = os.path.join(root, file)
                                break
                        if shp_path:
                            break

                    if shp_path:
                        st.session_state["uploaded_streams_gdf"] = gpd.read_file(shp_path)
                    else:
                        st.error("❌ No .shp file found inside the uploaded ZIP archive.")
            except Exception as e:
                st.error(f"❌ Failed to parse uploaded stream ZIP shapefile: {e}")
        elif stream_option == "None":
            st.session_state.pop("burn_streams_bytes", None)
            st.session_state.pop("uploaded_streams_gdf", None)

        st.markdown("---")
        st.subheader("2. Outlet Location (Native Coordinates)")
        crs_label = str(st.session_state.get("target_crs", "Projected CRS"))
        outlet_x = st.number_input(
            f"Outlet Easting / X ({crs_label})",
            value=st.session_state.get("outlet_x", 0.0),
            format="%.2f",
        )
        outlet_y = st.number_input(
            f"Outlet Northing / Y ({crs_label})",
            value=st.session_state.get("outlet_y", 0.0),
            format="%.2f",
        )
        st.session_state["outlet_x"] = outlet_x
        st.session_state["outlet_y"] = outlet_y

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            zoom_click = st.button(
                "🔍 Zoom to Terrain", use_container_width=True
            )
            if zoom_click and "map_bounds" in st.session_state:
                st.session_state["zoom_bounds"] = st.session_state[
                    "map_bounds"
                ]

        with col_btn2:
            # run_delineate = st.button(
            #     "⛰️ Run WBT Delineation",
            #     type="primary",
            #     use_container_width=True,
            # )
            
            ### start of the import 
            
            if exec_mode.startswith("Cloud-Based"):
                run_delineate = st.button("⛰️ Run WBT Delineation", type="primary", use_container_width=True, disabled=not terrain_files)
            else:
                if st.button("📦 Generate Local Batch Toolkit", type="primary", use_container_width=True, disabled=not terrain_files):
                    config_data = {
                        "outlet_x": outlet_x, "outlet_y": outlet_y,
                        "snap_dist": snap_dist, "threshold": threshold,
                        "stream_option": stream_option,
                        "target_crs": str(st.session_state.get("target_crs", "EPSG:32614")),
                    }

                    bat_content = r"""@echo off
setlocal
echo ===================================================
echo     Setting up Portable Python Environment
echo ===================================================
set PYTHON_DIR=%~dp0python_env
set PYTHON_EXE=%PYTHON_DIR%\python.exe

if not exist "%PYTHON_EXE%" (
    echo Downloading Portable Python Embeddable...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip' -OutFile 'python.zip'"
    if errorlevel 1 goto error
    
    echo Extracting Python...
    powershell -Command "Expand-Archive -Path 'python.zip' -DestinationPath '%PYTHON_DIR%'"
    if errorlevel 1 goto error
    del python.zip
    
    echo Downloading get-pip.py...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    if errorlevel 1 goto error
    
    echo Configuring pip pathways...
    powershell -Command "(Get-Content '%PYTHON_DIR%\python310._pth') -replace '#import site', 'import site' | Set-Content '%PYTHON_DIR%\python310._pth'"
    
    echo Installing pip...
    "%PYTHON_EXE%" get-pip.py
    if errorlevel 1 goto error
    del get-pip.py
)

echo.
echo Installing requirements (this may take a minute on the first run)...
"%PYTHON_EXE%" -m pip install --no-warn-script-location whitebox rasterio geopandas shapely numpy requests pyproj
if errorlevel 1 goto error

echo.
echo Executing Local Delineation Script...
"%PYTHON_EXE%" local_delineate.py
if errorlevel 1 goto error

rem Verify shapefile was genuinely created
if not exist "Shapefiles\watershed_boundary.shp" goto error

echo.
echo ===================================================
echo     SUCCESS: Watershed shapefiles created successfully!
echo ===================================================
pause
exit /b 0

:error
echo.
echo ===================================================
echo     ERROR: Process failed or shapefiles not found!
echo ===================================================
pause
exit /b 1
"""

                    py_content = r'''import os, glob, json, sys, zipfile, traceback
import numpy as np
import rasterio
import rasterio.features
from rasterio.merge import merge
import geopandas as gpd
import shapely.geometry as sg
import requests
from pyproj import Transformer
import whitebox

def safe_remove(file_path):
    """Safely deletes existing files or shapefile sidecars before overwriting."""
    if not file_path:
        return
    base, ext = os.path.splitext(file_path)
    if ext.lower() == '.shp':
        extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qpj', '.sbx', '.sbn']
        for e in extensions:
            p = base + e
            if os.path.exists(p):
                try:
                    os.remove(p)
                except PermissionError:
                    print(f"Warning: Could not remove locked file {p}. It may be open in another application.")
                except Exception:
                    pass
    else:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except PermissionError:
                print(f"Warning: Could not remove locked file {file_path}. It may be open in another application.")
            except Exception:
                pass


def fetch_texas_streams_rest(target_crs, native_bounds):
    min_x, min_y, max_x, max_y = native_bounds
    transformer = Transformer.from_crs(target_crs, "EPSG:4326", always_xy=True)
    corners = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
    lons, lats = zip(*[transformer.transform(x, y) for x, y in corners])
    xmin, xmax, ymin, ymax = min(lons), max(lons), min(lats), max(lats)

    url = (
        "https://feature.geographic.texas.gov/arcgis/rest/services/"
        "Hydrography/Tx_Rivers_Streams_Waterbodies/MapServer/1/query"
    )
    params = {
        "where": "1=1", "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
        "returnGeometry": "true", "f": "geojson", "outSR": "4326",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    
    response = requests.get(url, params=params, headers=headers, timeout=60)
    response.raise_for_status()
    geojson_data = response.json()

    if not geojson_data.get("features"): return None
    gdf = gpd.GeoDataFrame.from_features(geojson_data, crs="EPSG:4326")
    if gdf.empty: return None
    return gdf.to_crs(target_crs)

try:
    def run_local():
        with open('config.json', 'r') as f:
            config = json.load(f)
            
        os.makedirs('Shapefiles', exist_ok=True)
        os.makedirs('STREAMS', exist_ok=True)
        
        cwd = os.path.abspath('.')
        def abs_path(p): return os.path.join(cwd, os.path.normpath(p))

        
        # Read custom resample factor from text file if provided (defaults to 2)
        resample_factor = 2.0
        rf_file = abs_path('resample_factor.txt')
        if os.path.exists(rf_file):
            try:
                with open(rf_file, 'r') as rf:
                    val = float(rf.read().strip())
                    if val > 0:
                        resample_factor = val
                print(f"Loaded custom resample factor: {resample_factor}")
            except Exception as e:
                print(f"Could not parse resample_factor.txt ({e}), using default factor 2.0")
        else:
            print("resample_factor.txt not found, using default resample factor 2.0")
            
            
        
        #dem_files = glob.glob('DEM/*.tif') + glob.glob('DEM/*.tiff')
        
        # 1. Define generated output names to ignore from previous runs
        ignored_outputs = {
            'merged_dem.tif', 'filled_dem.tif', 'resampled_dem.tif', 
            'burned_dem.tif', 'd8_pointer.tif', 'flow_acc.tif', 
            'streams_raster.tif', 'watershed_raster.tif'
        }
        
        # 2. Find only raw input DEM tiles
        all_tifs = glob.glob('DEM/*.tif') + glob.glob('DEM/*.tiff')
        dem_files = [
            f for f in all_tifs 
            if os.path.basename(f).lower() not in ignored_outputs
        ]
        
        if not dem_files:
            print("ERROR: No .tif files found in the 'DEM' folder.")
            sys.exit(1)

        print(f"Found {len(dem_files)} DEM files. Processing...")
        
        target_dem = 'DEM/merged_dem.tif'
        if len(dem_files) > 1:
            safe_remove(target_dem)
            srcs = [rasterio.open(f) for f in dem_files]
            mosaic, out_trans = merge(srcs)
            out_meta = srcs[0].meta.copy()
            out_meta.update({"height": mosaic.shape[1], "width": mosaic.shape[2], "transform": out_trans})
            with rasterio.open(target_dem, 'w', **out_meta) as dest:
                dest.write(mosaic)
            for s in srcs: s.close()
        else:
            target_dem = dem_files[0]

        with rasterio.open(target_dem) as src:
            res_x, res_y = abs(src.transform[0]), abs(src.transform[4])
            crs_wkt = src.crs.to_wkt().lower() if src.crs else ""
            is_feet = "foot" in crs_wkt or "ft" in crs_wkt
            is_deg = src.crs.is_geographic if src.crs else False
            native_bounds = src.bounds

        wbt = whitebox.WhiteboxTools()
        wbt.set_verbose_mode(True)
        
       
        wbt.set_working_dir(cwd)

        print("Filling DEM Depressions...")
        
        target_dem = abs_path(target_dem)
        filled_dem = abs_path('DEM/filled_dem.tif')
        
        # 1. Check if the file size is greater than 3 GB (3 * 1024 * 1024 * 1024 bytes)
        file_size_bytes = os.path.getsize(target_dem)
        three_gb = 3 * 1024 * 1024 * 1024
        
        from rasterio.enums import Resampling
        import gc

        if file_size_bytes > three_gb:
            print(f"DEM size ({file_size_bytes / (1024**3):.2f} GB) exceeds 3 GB limit. Resampling with Rasterio...")
            
            resampled_dem = abs_path('DEM/resampled_dem.tif')
            safe_remove(resampled_dem)
            
            with rasterio.open(target_dem) as src:
                current_res_x = abs(src.transform[0])
                current_res_y = abs(src.transform[4])
                new_res_x = current_res_x * resample_factor
                
                print(f"Changing pixel size from {current_res_x:.2f} to {new_res_x:.2f} (factor = {resample_factor})")
                
                # Calculate output pixel dimensions (reduced 36x for factor 6.0)
                new_height = int(round(src.height / resample_factor))
                new_width = int(round(src.width / resample_factor))
                
                # Recalculate affine transform
                new_transform = src.transform * src.transform.scale(
                    (src.width / new_width),
                    (src.height / new_height)
                )
                
                out_meta = src.meta.copy()
                out_meta.update({
                    "driver": "GTiff",
                    "height": new_height,
                    "width": new_width,
                    "transform": new_transform,
                    "tiled": True,
                    "blockxsize": 512,
                    "blockysize": 512,
                    "compress": "lzw"
                })
                
                # Decimated Read: Rasterio downsamples on-the-fly while reading from disk.
                # RAM allocation is limited ONLY to the output array size (~110 MB).
                data = src.read(
                    out_shape=(src.count, new_height, new_width),
                    resampling=Resampling.bilinear
                )
                
                with rasterio.open(resampled_dem, 'w', **out_meta) as dest:
                    dest.write(data)
                    
                del data
                gc.collect()
                
            # Point subsequent WhiteboxTools steps to the light, downsampled DEM
            target_dem = resampled_dem
        
        # if file_size_bytes > three_gb:
        #     print(f"DEM size ({file_size_bytes / (1024**3):.2f} GB) exceeds 3 GB limit. Resampling...")
            
        #     # 2. Open the DEM to find its current pixel size
        #     with rasterio.open(target_dem) as src:
        #         current_res_x = abs(src.transform[0])  # Pixel width
        #         current_res_y = abs(src.transform[4])  # Pixel height
            
        #     # 3. Double the resolution size
        #     new_res_x = current_res_x * resample_factor
            
        #     # 4. Create a temporary path for the downsampled DEM
        #     resampled_dem = abs_path('DEM/resampled_dem.tif')
        #     safe_remove(resampled_dem)
            
        #     print(f"Changing pixel size from {current_res_x:.2f} to {new_res_x:.2f} (factor = {resample_factor})")
            
        #     # Run WhiteboxTools resample
        #     wbt.resample(
        #         inputs=target_dem,
        #         output=resampled_dem,
        #         cell_size=new_res_x,
        #         method="bilinear"  # Best method for continuous elevation data
        #     )
            
        #     # Point the filling tool to the new, lighter DEM
        #     target_dem = resampled_dem
        
        # 5. Run the depression filling tool
        # print("Filling DEM Depressions...")
        # safe_remove(filled_dem)
        # wbt.fill_depressions(dem=target_dem, output=filled_dem, fix_flats=True,flat_increment=None)
        
        print("Running FillDepressionsWangAndLiu...")
        safe_remove(filled_dem)
        wbt.fill_depressions_wang_and_liu(
            dem=target_dem, 
            output=filled_dem, 
            fix_flats=True, 
            flat_increment=None
        )
        
        # Optional: Clean up the temporary resampled file if it was created
        if 'resampled_dem' in locals() and os.path.exists(resampled_dem):
            safe_remove(resampled_dem)
        
        

        dem_to_use = filled_dem

        stream_option = config.get('stream_option', 'None')
        burn_streams_path = None
        
        if stream_option == "Upload Stream Shapefile (ZIP)":
            streams_zip_path = abs_path('STREAMS/streams.zip')
            if os.path.exists(streams_zip_path):
                print("Extracting uploaded streams...")
                with zipfile.ZipFile(streams_zip_path, 'r') as zf:
                    zf.extractall(abs_path('STREAMS/extracted'))
                shp_files = glob.glob(abs_path('STREAMS/extracted/**/*.shp'), recursive=True)
                if shp_files:
                    burn_streams_path = shp_files[0]
                    burn_gdf = gpd.read_file(burn_streams_path)
                    if burn_gdf.crs != config['target_crs']:
                        burn_gdf = burn_gdf.to_crs(config['target_crs'])
                        burn_streams_path = abs_path('STREAMS/reproj_streams.shp')
                        safe_remove(burn_streams_path)
                        burn_gdf.to_file(burn_streams_path)

        elif stream_option == "Use Texas Gov Dataset (ArcGIS REST)":
            print("Querying Texas Hydrography ArcGIS REST Service...")
            tx_gdf = fetch_texas_streams_rest(config['target_crs'], native_bounds)
            if tx_gdf is not None and not tx_gdf.empty:
                burn_streams_path = abs_path('STREAMS/tx_rest_streams.shp')
                safe_remove(burn_streams_path)
                tx_gdf.to_file(burn_streams_path)
                print("SUCCESS: Downloaded Texas streams to STREAMS folder.")

        if burn_streams_path and os.path.exists(burn_streams_path):
            print("Burning streams into DEM...")
            burned_dem = abs_path('DEM/burned_dem.tif')
            safe_remove(burned_dem)
            wbt.fill_burn(dem=filled_dem, streams=burn_streams_path, output=burned_dem)
            if os.path.exists(burned_dem):
                dem_to_use = burned_dem

        print("Calculating Flow Direction and Accumulation...")
        d8_pointer = abs_path('DEM/d8_pointer.tif')
        flow_acc = abs_path('DEM/flow_acc.tif')
        safe_remove(d8_pointer)
        safe_remove(flow_acc)
        wbt.d8_pointer(dem=dem_to_use, output=d8_pointer)
        wbt.d8_flow_accumulation(i=dem_to_use, output=flow_acc)

        print("Extracting Streams...")
        streams_raster = abs_path('DEM/streams_raster.tif')
        streams_vector = abs_path('Shapefiles/streams_network.shp')
        safe_remove(streams_raster)
        safe_remove(streams_vector)
        wbt.extract_streams(flow_accum=flow_acc, output=streams_raster, threshold=config['threshold'])
        wbt.raster_streams_to_vector(streams=streams_raster, d8_pntr=d8_pointer, output=streams_vector)
        
        # Explicitly assign target CRS to stream network so it includes a valid .prj file
        if os.path.exists(streams_vector):
            gdf_st = gpd.read_file(streams_vector)
            gdf_st = gdf_st.set_crs(config['target_crs'], allow_override=True)
            safe_remove(streams_vector)
            gdf_st.to_file(streams_vector)
        
    

        x_coord, y_coord = config['outlet_x'], config['outlet_y']
        print(f"Snapping Pour Point near ({x_coord}, {y_coord})...")
        pour_pts_shp = abs_path('DEM/pour_point.shp')
        safe_remove(pour_pts_shp)
        gpd.GeoDataFrame(geometry=[sg.Point(x_coord, y_coord)], crs=config['target_crs']).to_file(pour_pts_shp)

        snapped_pour_pts = abs_path('Shapefiles/snapped_pour_points.shp')
        safe_remove(snapped_pour_pts)
        effective_snap = float(config['snap_dist'])
        if is_deg:
            effective_snap = effective_snap / (364173.0 if is_feet else 111000.0)
            
        wbt.snap_pour_points(pour_pts=pour_pts_shp, flow_accum=flow_acc, output=snapped_pour_pts, snap_dist=effective_snap)

        if not os.path.exists(snapped_pour_pts):
            print(f"\nERROR: WhiteboxTools failed to snap the pour point. Ensure coordinates ({x_coord}, {y_coord}) are inside the DEM.")
            sys.exit(1)

        print("Delineating Catchment...")
        watershed_raster = abs_path('DEM/watershed_raster.tif')
        safe_remove(watershed_raster)
        wbt.watershed(d8_pntr=d8_pointer, pour_pts=snapped_pour_pts, output=watershed_raster)

        if not os.path.exists(watershed_raster):
            print("\nERROR: WhiteboxTools failed to create the watershed raster.")
            sys.exit(1)

        print("Exporting Watershed Boundary Shapefile...")
        with rasterio.open(watershed_raster) as src_ws:
            ws_data = src_ws.read(1)
            ws_mask = ws_data > 0
            shapes = rasterio.features.shapes(ws_data, mask=ws_mask, transform=src_ws.transform)
            ws_geoms = [sg.shape(s) for s, v in shapes if v > 0]
            
        if not ws_geoms:
            print("ERROR: Empty watershed resulting from delineation. Adjust threshold or outlet coordinates.")
            sys.exit(1)

        
        ws_gdf = gpd.GeoDataFrame(geometry=ws_geoms, crs=config['target_crs'])
        ws_path = abs_path('Shapefiles/watershed_boundary.shp')
        safe_remove(ws_path)
        ws_gdf.to_file(ws_path)
        
        if os.path.exists(streams_vector):
            raw_st = gpd.read_file(streams_vector).set_crs(config['target_crs'], allow_override=True)
            clipped_st = gpd.clip(raw_st, ws_gdf)
            clipped_st = clipped_st[clipped_st.geometry.type.isin(["LineString", "MultiLineString"])]
            if not clipped_st.empty:
                clip_path = abs_path('Shapefiles/streams_clipped.shp')
                safe_remove(clip_path)
                clipped_st.to_file(clip_path)

        print("Calculating Longest Flow Path...")
        flowpath_shp = abs_path('Shapefiles/longest_flowpath.shp')
        safe_remove(flowpath_shp)
        wbt.longest_flowpath(dem=dem_to_use, basins=watershed_raster, output=flowpath_shp)
        
        # Explicitly assign target CRS to longest flow path so it includes a valid .prj file
        if os.path.exists(flowpath_shp):
            gdf_fp = gpd.read_file(flowpath_shp)
            gdf_fp = gdf_fp.set_crs(config['target_crs'], allow_override=True)
            safe_remove(flowpath_shp)
            gdf_fp.to_file(flowpath_shp)
            
        
        print("\n" + "="*55)
        print("            HYDROLOGIC CHARACTERISTICS SUMMARY")
        print("="*55)
        
        try:
            # 1. Ensure watershed GeoDataFrame has the target CRS set
            if ws_gdf.crs is None:
                ws_gdf = ws_gdf.set_crs(config['target_crs'])
            
            # Safely reproject watershed to a metric CRS (UTM) for accurate area calculation
            if ws_gdf.crs.is_geographic:
                ws_metric = ws_gdf.to_crs(ws_gdf.estimate_utm_crs())
            else:
                # If already projected (e.g. State Plane feet), route through WGS84 to estimate metric UTM
                ws_metric = ws_gdf.to_crs("EPSG:4326").to_crs(ws_gdf.to_crs("EPSG:4326").estimate_utm_crs())
                
            area_sqm = ws_metric.geometry.area.sum()
            area_mi2 = area_sqm * 3.86102e-7
            area_acres = area_sqm * 0.000247105
            
            print(f"Drainage Area:       {area_mi2:.4f} mi²")
            print(f"                     {area_acres:.2f} acres")
            
            # 2. Handle Longest Flow Path shapefile (assigning missing CRS from config)
            if os.path.exists(flowpath_shp):
                lfp_gdf = gpd.read_file(flowpath_shp)
                if lfp_gdf.crs is None:
                    lfp_gdf = lfp_gdf.set_crs(config['target_crs'])
                
                if lfp_gdf.crs.is_geographic:
                    lfp_metric = lfp_gdf.to_crs(lfp_gdf.estimate_utm_crs())
                else:
                    lfp_metric = lfp_gdf.to_crs("EPSG:4326").to_crs(lfp_gdf.to_crs("EPSG:4326").estimate_utm_crs())
                    
                len_m = lfp_metric.geometry.length.sum()
                len_mi = len_m * 0.000621371
                len_yd = len_m * 1.09361
                
                print(f"Longest Flow Path:   {len_mi:.4f} miles")
                print(f"                     {len_yd:.2f} yards")
            else:
                print("Longest Flow Path:   Not generated.")
                
        except Exception as e:
            print(f"Could not compute hydrologic characteristics: {e}")
            
        
        print("="*55 + "\n")
        
        print("\n===================================================")
        print("     SUCCESS: GIS Files generated in 'Shapefiles' folder.")
        print("===================================================\n")

    if __name__ == '__main__':
        run_local()

except Exception as e:
    print(f"\nCRITICAL ERROR: {str(e)}")
    traceback.print_exc()
    sys.exit(1)
'''

                    cn_bat_content = r"""@echo off
setlocal
echo ===================================================
echo     Setting up Portable Python Environment for CN
echo ===================================================
set PYTHON_DIR=%~dp0python_env
set PYTHON_EXE=%PYTHON_DIR%\python.exe

if not exist "%PYTHON_EXE%" (
    echo Downloading Portable Python Embeddable...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip' -OutFile 'python.zip'"
    if errorlevel 1 goto error
    
    echo Extracting Python...
    powershell -Command "Expand-Archive -Path 'python.zip' -DestinationPath '%PYTHON_DIR%'"
    if errorlevel 1 goto error
    del python.zip
    
    echo Downloading get-pip.py...
    powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py'"
    if errorlevel 1 goto error
    
    echo Configuring pip pathways...
    powershell -Command "(Get-Content '%PYTHON_DIR%\python310._pth') -replace '#import site', 'import site' | Set-Content '%PYTHON_DIR%\python310._pth'"
    
    echo Installing pip...
    "%PYTHON_EXE%" get-pip.py
    if errorlevel 1 goto error
    del get-pip.py
)

echo.
echo Installing requirements (this may take a minute on first run)...
"%PYTHON_EXE%" -m pip install --no-warn-script-location rasterio geopandas shapely numpy requests pyproj pandas
if errorlevel 1 goto error

echo.
echo Executing Local Curve Number Script...
"%PYTHON_EXE%" local_cn.py
if errorlevel 1 goto error

rem Verify shapefile was genuinely created
if not exist "Shapefiles\curve_number_polygons.shp" goto error

echo.
echo ===================================================
echo     SUCCESS: Curve Number shapefiles created!
echo ===================================================
pause
exit /b 0

:error
echo.
echo ===================================================
echo     ERROR: Process failed or shapefiles not found!
echo ===================================================
pause
exit /b 1
"""

                    cn_py_content = r'''import os, sys, json, tempfile, traceback, requests, io
import numpy as np
import pandas as pd
import geopandas as gpd
import shapely.geometry as sg
import shapely.ops
import rasterio
import rasterio.features
from rasterio.windows import Window

def safe_remove(file_path):
    """Safely deletes existing files or shapefile sidecars before overwriting."""
    if not file_path:
        return
    base, ext = os.path.splitext(file_path)
    if ext.lower() == '.shp':
        extensions = ['.shp', '.shx', '.dbf', '.prj', '.cpg', '.qpj', '.sbx', '.sbn']
        for e in extensions:
            p = base + e
            if os.path.exists(p):
                try:
                    os.remove(p)
                except PermissionError:
                    print(f"Warning: Could not remove locked file {p}. It may be open in another application.")
                except Exception:
                    pass
    else:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except PermissionError:
                print(f"Warning: Could not remove locked file {file_path}. It may be open in another application.")
            except Exception:
                pass


def extract_local_nlcd_windowed(src, aoi_proj):
    xmin, ymin, xmax, ymax = aoi_proj.total_bounds
    win = src.window(xmin, ymin, xmax, ymax)
    full_canvas = Window(0, 0, src.width, src.height)
    win = win.intersection(full_canvas)

    win_trans = src.window_transform(win)
    data = src.read(1, window=win)
    height, width = data.shape

    if height == 0 or width == 0:
        raise ValueError("AOI extent has zero area overlap with the raster canvas.")

    geoms = [g for g in aoi_proj.geometry if g is not None and not g.is_empty]
    inside_mask = rasterio.features.geometry_mask(geoms, out_shape=(height, width), transform=win_trans, invert=True, all_touched=True)

    valid_mask = inside_mask & (data > 0) & (data < 255)
    if not np.any(valid_mask):
        raise ValueError("No valid NLCD land cover pixels found within watershed boundary.")

    results = (
        {"properties": {"land_use": str(int(val))}, "geometry": geom}
        for geom, val in rasterio.features.shapes(data, mask=valid_mask, transform=win_trans)
    )

    nlcd_gdf = gpd.GeoDataFrame.from_features(list(results), crs=src.crs)
    nlcd_gdf = nlcd_gdf.dissolve(by="land_use").reset_index()
    return nlcd_gdf


def fetch_nlcd_dataset(aoi_gdf):
    aoi_5070 = aoi_gdf.to_crs("EPSG:5070")
    bounds = aoi_5070.total_bounds

    xmin, ymin = bounds[0] - 30, bounds[1] - 30
    xmax, ymax = bounds[2] + 30, bounds[3] + 30

    width = int((xmax - xmin) / 30)
    height = int((ymax - ymin) / 30)
    bbox_str = f"{xmin},{ymin},{xmax},{ymax}"

    wcs_url = (
        "https://www.mrlc.gov/geoserver/ows?version=1.1.0&SERVICE=WCS&VERSION=1.0.0&"
        "request=GetCoverage&format=GeoTIFF&coverage=mrlc_download:NLCD_2021_Land_Cover_L48&"
        f"crs=EPSG:5070&width={width}&height={height}&bbox={bbox_str}"
    )

    temp_fd, temp_path = tempfile.mkstemp(suffix=".tif")
    os.close(temp_fd)

    try:
        response = requests.get(wcs_url, stream=True, timeout=120)
        response.raise_for_status()

        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        with rasterio.open(temp_path) as src:
            nlcd_gdf = extract_local_nlcd_windowed(src, aoi_5070)
            return nlcd_gdf
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def download_ssurgo_extended(aoi_gdf):
    aoi_4326 = aoi_gdf.to_crs("EPSG:4326")
    bounds = aoi_4326.total_bounds
    bbox_str = f"{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}"

    wfs_url = "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84GEOGRAPHIC.wfs".strip()
    params = {
        "SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature",
        "TYPENAME": "mapunitpolyextended", "SRSNAME": "EPSG:4326", "BBOX": bbox_str,
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    response = requests.get(wfs_url, params=params, headers=headers, timeout=60)
    response.raise_for_status()

    gml_bytes = io.BytesIO(response.content)
    ssurgo_gdf = gpd.read_file(gml_bytes)

    if ssurgo_gdf.empty:
        return ssurgo_gdf

    ssurgo_gdf.geometry = ssurgo_gdf.geometry.map(lambda geom: (shapely.ops.transform(lambda x, y: (y, x), geom) if geom else None))
    ssurgo_gdf.set_crs("EPSG:4326", inplace=True)
    return ssurgo_gdf


def clean_hsg(val):
    if pd.isna(val) or val is None: return ""
    val_str = str(val).strip().upper()
    if val_str in ["NONE", "NULL", "NAN", "", "0"]: return ""
    if "/" in val_str:
        parts = [p.strip() for p in val_str.split("/") if p.strip()]
        return parts[-1] if parts else ""
    return val_str


def calculate_weighted_cn_and_c(aoi_gdf, nlcd_gdf, ssurgo_gdf, lookup_csv_path):
    target_crs = aoi_gdf.crs if (aoi_gdf.crs and aoi_gdf.crs.is_projected) else "EPSG:5070"
    aoi_proj = aoi_gdf.to_crs(target_crs)
    nlcd_proj = nlcd_gdf.to_crs(target_crs)
    ssurgo_proj = ssurgo_gdf.to_crs(target_crs)

    ssurgo_clipped = gpd.clip(ssurgo_proj, aoi_proj)
    nlcd_clipped = gpd.clip(nlcd_proj, aoi_proj)

    if ssurgo_clipped.empty or nlcd_clipped.empty:
        raise ValueError("SSURGO or NLCD dataset returned empty geometry when clipped.")

    hyg_col = next((c for c in ssurgo_clipped.columns if c.lower() in ["hydgrpdcd", "hydgrp", "hyg"]), None)
    ssurgo_clipped["hyg_clean"] = ssurgo_clipped[hyg_col].apply(clean_hsg) if hyg_col else ""
    nlcd_clipped["land_use_clean"] = nlcd_clipped["land_use"].astype(str).str.strip().str.split(".").str[0]

    ssurgo_grouped = ssurgo_clipped[["hyg_clean", "geometry"]].dissolve(by="hyg_clean").reset_index()
    nlcd_grouped = nlcd_clipped[["land_use_clean", "geometry"]].dissolve(by="land_use_clean").reset_index()

    final_intersect = gpd.overlay(ssurgo_grouped, nlcd_grouped, how="intersection")

    if final_intersect.empty:
        raise ValueError("Spatial intersection yielded empty geometry.")

    final_intersect["grid_code"] = final_intersect["land_use_clean"] + "_" + final_intersect["hyg_clean"]
    lookup_df = pd.read_csv(lookup_csv_path)
    lookup_df.columns = [c.strip().lower() for c in lookup_df.columns]
    lookup_df["grid_code"] = lookup_df["grid_code"].astype(str).str.strip()
    lookup_df["cn"] = pd.to_numeric(lookup_df["cn"], errors="coerce")
    lookup_df["c"] = pd.to_numeric(lookup_df["c"], errors="coerce")

    merged = final_intersect.merge(lookup_df, on="grid_code", how="left")
    merged["area_sqm"] = merged.geometry.area
    total_area = merged["area_sqm"].sum()

    if total_area == 0:
        raise ValueError("Total area of intersected polygon features is zero.")

    merged["cn"] = merged["cn"].fillna(0)
    merged["area_x_cn"] = merged["area_sqm"] * merged["cn"]
    weighted_cn = merged["area_x_cn"].sum() / total_area

    merged["c"] = merged["c"].fillna(0)
    merged["area_x_c"] = merged["area_sqm"] * merged["c"]
    weighted_c = merged["area_x_c"].sum() / total_area

    return weighted_cn, weighted_c, merged


try:
    def run_local_cn():
        cwd = os.path.abspath('.')
        def abs_path(p): return os.path.join(cwd, os.path.normpath(p))

        ws_shp = abs_path("Shapefiles/watershed_boundary.shp")
        if not os.path.exists(ws_shp):
            print("\nERROR: Watershed boundary shapefile not found at 'Shapefiles/watershed_boundary.shp'.")
            print("Please run 'execute.bat' first to delineate the watershed boundary.")
            sys.exit(1)

        print(f"Loading watershed boundary from '{ws_shp}'...")
        aoi_gdf = gpd.read_file(ws_shp)

        print("\n1/3 Downloading 2021 NLCD Land Cover Dataset (MRLC WCS)...")
        nlcd_gdf = fetch_nlcd_dataset(aoi_gdf)
        nlcd_out = abs_path("Shapefiles/nlcd_landuse.shp")
        safe_remove(nlcd_out)
        nlcd_gdf.to_file(nlcd_out)
        print(f"      Saved NLCD layer to '{nlcd_out}'")

        print("\n2/3 Downloading SSURGO Soil Dataset (USDA WFS)...")
        ssurgo_gdf = download_ssurgo_extended(aoi_gdf)
        if ssurgo_gdf.empty:
            print("ERROR: SSURGO returned no soil polygon data for this watershed boundary.")
            sys.exit(1)

        ssurgo_out = abs_path("Shapefiles/ssurgo_soils.shp")
        safe_remove(ssurgo_out)
        ssurgo_gdf.to_file(ssurgo_out)
        print(f"      Saved SSURGO layer to '{ssurgo_out}'")

        print("\n3/3 Calculating Curve Number and Runoff Coefficient...")
        lookup_csv = abs_path("NLCD_SHG_CN_lookup.csv")
        if not os.path.exists(lookup_csv):
            print(f"ERROR: Lookup table '{lookup_csv}' not found. Ensure NLCD_SHG_CN_lookup.csv exists in the root folder.")
            sys.exit(1)

        weighted_cn, weighted_c, merged_gdf = calculate_weighted_cn_and_c(aoi_gdf, nlcd_gdf, ssurgo_gdf, lookup_csv)
        cn_out = abs_path("Shapefiles/curve_number_polygons.shp")
        safe_remove(cn_out)
        merged_gdf.to_file(cn_out)
        print(f"      Saved CN Polygons layer to '{cn_out}'")

        target_crs = merged_gdf.crs
        if target_crs is None or target_crs.is_geographic:
            calc_gdf = merged_gdf.to_crs("EPSG:5070")
        else:
            calc_gdf = merged_gdf.copy()

        crs_wkt = calc_gdf.crs.to_wkt().lower()
        is_feet = "foot" in crs_wkt or "ft" in crs_wkt

        calc_gdf["area_sqm"] = calc_gdf.geometry.area
        if is_feet:
            calc_gdf["area_acres"] = calc_gdf["area_sqm"] / 43560.0
        else:
            calc_gdf["area_acres"] = calc_gdf["area_sqm"] / 4046.8564224

        total_acres = calc_gdf["area_acres"].sum()
        total_sqmi = total_acres / 640.0

        print("\n" + "="*65)
        print("        CURVE NUMBER & RUNOFF COEFFICIENT SUMMARY")
        print("="*65)
        print(f"Composite Curve Number (CN): {weighted_cn:.2f}")
        print(f"Composite Runoff Coeff. (C): {weighted_c:.2f}")
        print(f"Total Watershed Area:        {total_acres:.2f} acres ({total_sqmi:.4f} mi²)")
        print("="*65)
        print("\nDETAILED BREAKDOWN BY LAND USE & SOIL GROUP:")
        print("-" * 65)

        calc_gdf["% Area"] = (calc_gdf["area_acres"] / total_acres * 100).round(2)
        calc_gdf["Area (Acres)"] = calc_gdf["area_acres"].round(2)

        rename_dict = {
            "land_use_clean": "NLCD",
            "hyg_clean": "HSG",
            "grid_code": "GridCode",
            "cn": "CN",
            "c": "C",
        }
        disp_df = calc_gdf.rename(columns=rename_dict)
        cols = [c for c in ["NLCD", "HSG", "GridCode", "CN", "C", "Area (Acres)", "% Area"] if c in disp_df.columns]
        print(disp_df[cols].to_string(index=False))
        print("="*65 + "\n")

    if __name__ == '__main__':
        run_local_cn()

except Exception as e:
    print(f"\nCRITICAL ERROR: {str(e)}")
    traceback.print_exc()
    sys.exit(1)
'''


                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                        zipf.writestr("DEM/", "")
                        zipf.writestr("Shapefiles/", "")
                        zipf.writestr("STREAMS/", "")
                        zipf.writestr("Step_1_Watershed_Delineate.bat", bat_content)
                        zipf.writestr("local_delineate.py", py_content)
                        zipf.writestr("Step_2_CN_&_C_calc.bat", cn_bat_content)
                        zipf.writestr("local_cn.py", cn_py_content)
                        zipf.writestr("config.json", json.dumps(config_data, indent=4))
                        zipf.writestr("resample_factor.txt", "2")

                        if "lookup_upload_file" in st.session_state and st.session_state.lookup_upload_file is not None:
                            lookup_upload = st.session_state.lookup_upload_file
                            zipf.writestr("NLCD_SHG_CN_lookup.csv", lookup_upload.getvalue())
                        elif os.path.exists("NLCD_SHG_CN_lookup.csv"):
                            with open("NLCD_SHG_CN_lookup.csv", "rb") as f_lk:
                                zipf.writestr("NLCD_SHG_CN_lookup.csv", f_lk.read())
                        else:
                            default_csv = (
                                "grid_code,cn,c\n"
                                "11_A,0,0.00\n11_B,0,0.00\n11_C,0,0.00\n11_D,0,0.00\n"
                                "21_A,39,0.15\n21_B,61,0.20\n21_C,74,0.25\n21_D,80,0.30\n"
                                "22_A,54,0.22\n22_B,70,0.28\n22_C,80,0.35\n22_D,85,0.40\n"
                                "23_A,77,0.45\n23_B,85,0.55\n23_C,90,0.65\n23_D,92,0.75\n"
                                "24_A,89,0.70\n24_B,92,0.80\n24_C,94,0.85\n24_D,95,0.90\n"
                                "31_A,77,0.30\n31_B,86,0.40\n31_C,91,0.50\n31_D,94,0.60\n"
                                "41_A,36,0.10\n41_B,60,0.15\n41_C,73,0.20\n41_D,79,0.25\n"
                                "42_A,30,0.10\n42_B,55,0.15\n42_C,70,0.20\n42_D,77,0.25\n"
                                "43_A,43,0.12\n43_B,65,0.18\n43_C,76,0.22\n43_D,82,0.28\n"
                                "52_A,35,0.12\n52_B,56,0.18\n52_C,70,0.22\n52_D,77,0.28\n"
                                "71_A,39,0.15\n71_B,61,0.22\n71_C,74,0.28\n71_D,80,0.35\n"
                                "81_A,39,0.15\n81_B,61,0.22\n81_C,74,0.28\n81_D,80,0.35\n"
                                "82_A,67,0.25\n82_B,78,0.32\n82_C,85,0.40\n82_D,89,0.48\n"
                                "90_A,30,0.05\n90_B,55,0.10\n90_C,70,0.15\n90_D,77,0.20\n"
                                "95_A,30,0.05\n95_B,55,0.10\n95_C,70,0.15\n95_D,77,0.20\n"
                            )
                            zipf.writestr("NLCD_SHG_CN_lookup.csv", default_csv)


                        for dem_item in st.session_state.get("dem_files_data", []):
                            if dem_item["is_xml"]:
                                with tempfile.TemporaryDirectory() as temp_xml_dir:
                                    xml_buf = io.BytesIO(dem_item["bytes"])
                                    temp_tif = os.path.join(temp_xml_dir, "converted_xml.tif")
                                    parse_landxml_to_geotiff(xml_buf, temp_tif)
                                    with open(temp_tif, "rb") as f:
                                        zipf.writestr(f"DEM/{dem_item['name'].replace('.xml', '.tif')}", f.read())
                            else:
                                zipf.writestr(f"DEM/{dem_item['name']}", dem_item["bytes"])

                        if stream_burn_file is not None:
                            zipf.writestr("STREAMS/streams.zip", stream_burn_file.getvalue())

                    st.download_button(
                        label="⬇️ Download Complete Local Toolkit (.zip)",
                        data=zip_buffer.getvalue(),
                        file_name="Local_Delineation_Kit.zip",
                        mime="application/zip",
                        type="primary",
                        use_container_width=True,
                    )
                run_delineate = False

            
            
            
            ### end of the imports

        if "watershed_results" in st.session_state:
            st.markdown("---")
            st.subheader("📊 Watershed Metrics")
            res = st.session_state["watershed_results"]
            st.metric(
                "Drainage Area",
                f"{res['area_sqmi']:.3f} mi²",
                f"{res['area_acres']:.1f} Acres",
            )
            st.metric(
                "Longest Flow Path",
                f"{res['longest_flow_mi']:.2f} Miles",
                f"{res['longest_flow_ft']:.1f} Feet",
            )

            st.markdown("---")
            st.subheader("📥 Export Portal")
            if "shp_zip_bytes" in st.session_state:
                st.download_button(
                    label="📦 Download All Project GIS Files (ZIP)",
                    data=st.session_state["shp_zip_bytes"],
                    file_name="watershed_project_all_files.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

    with col_map:
        st.subheader("Interactive Map Viewer")
        marker_lat, marker_lon = 30.2672, -97.7431
        if "target_crs" in st.session_state:
            try:
                transformer_to_wgs84 = Transformer.from_crs(
                    st.session_state["target_crs"], "EPSG:4326", always_xy=True
                )
                marker_lon, marker_lat = transformer_to_wgs84.transform(
                    st.session_state.get("outlet_x", 0.0),
                    st.session_state.get("outlet_y", 0.0),
                )
            except Exception:
                pass

        m = folium.Map(location=[marker_lat, marker_lon], zoom_start=13)

        legend_css = """
        <style>
        svg text {
            font-weight: 900 !important;
            fill: #000000 !important;
            text-shadow: 
                2px 0px 0px #FFFFFF, -2px 0px 0px #FFFFFF, 
                0px 2px 0px #FFFFFF, 0px -2px 0px #FFFFFF, 
                1px 1px 0px #FFFFFF, -1px -1px 0px #FFFFFF, 
                1px -1px 0px #FFFFFF, -1px 1px 0px #FFFFFF !important;
        }
        </style>
        """
        m.get_root().header.add_child(folium.Element(legend_css))

        if "processed_dems" in st.session_state:
            p_dems = st.session_state["processed_dems"]
            g_min = st.session_state["global_min"]
            g_max = st.session_state["global_max"]

            #terrain_cmap = cm.get_cmap("terrain", 15)
            terrain_cmap = mpl.colormaps['terrain'].resampled(15)
            hex_colors = [
                mcolors.to_hex(terrain_cmap(i)) for i in np.linspace(0, 1, 15)
            ]

            if g_min < float("inf") and g_max > float("-inf"):
                colormap = cmp.LinearColormap(
                    colors=hex_colors,
                    vmin=float(g_min),
                    vmax=float(g_max),
                    caption="Elevation (Project Units)",
                )
                colormap.add_to(m)

                for dem in p_dems:
                    dst_height, dst_width = dem["array"].shape
                    rgba_img = np.zeros(
                        (dst_height, dst_width, 4), dtype=np.uint8
                    )

                    if dem["mask"].any() and g_max > g_min:
                        norm = (
                            (dem["array"] - g_min)
                            / (g_max - g_min)
                            * 255
                        ).clip(0, 255).astype(np.uint8)
                        
                        #colored = cm.terrain(norm / 255.0) * 255
                        colored = terrain_cmap(norm / 255.0) * 255
                        rgba_img = colored.astype(np.uint8)

                    rgba_img[..., 3] = np.where(dem["mask"], 160, 0)

                    folium.raster_layers.ImageOverlay(
                        image=rgba_img,
                        bounds=dem["bounds"],
                        opacity=0.9,
                        name=dem["name"],
                    ).add_to(m)

        if "stream_gdf" in st.session_state:
            folium.GeoJson(
                st.session_state["stream_gdf"].to_crs("EPSG:4326"),
                name="Watershed Streams (Clipped)",
                style_function=lambda x: {
                    "color": "blue",
                    "weight": 1.5,
                    "opacity": 0.8,
                },
            ).add_to(m)

        if "aoi_gdf" in st.session_state:
            folium.GeoJson(
                st.session_state["aoi_gdf"].to_crs("EPSG:4326"),
                name="Watershed Boundary",
                style_function=lambda x: {
                    "color": "red",
                    "fillColor": "red",
                    "fillOpacity": "0.15",
                    "weight": 2,
                },
            ).add_to(m)

        if "flowpath_gdf" in st.session_state:
            folium.GeoJson(
                st.session_state["flowpath_gdf"].to_crs("EPSG:4326"),
                name="Longest Flow Path",
                style_function=lambda x: {"color": "orange", "weight": 3},
            ).add_to(m)

        if "uploaded_streams_gdf" in st.session_state:
            try:
                uploaded_4326 = st.session_state[
                    "uploaded_streams_gdf"
                ].to_crs("EPSG:4326")
                folium.GeoJson(
                    uploaded_4326,
                    name="Burn-in Streams",
                    style_function=lambda x: {
                        "color": "#00FFFF",
                        "weight": 3,
                        "opacity": 0.9,
                        "dashArray": "4, 4",
                    },
                    tooltip="Burn-in Stream Channel",
                ).add_to(m)
            except Exception as e:
                st.warning(f"Could not render burn-in streams on map: {e}")

        if "downslope_gdf" in st.session_state:
            folium.GeoJson(
                st.session_state["downslope_gdf"].to_crs("EPSG:4326"),
                name="Downslope Trace Water Path",
                style_function=lambda x: {
                    "color": "purple",
                    "weight": 2.5,
                    "dashArray": "5, 5",
                },
            ).add_to(m)

        folium.Marker(
            location=[marker_lat, marker_lon],
            popup="Outlet / Pour Point",
            icon=folium.Icon(color="red", icon="flag"),
        ).add_to(m)

        if (
            "zoom_bounds" in st.session_state
            and st.session_state["zoom_bounds"] is not None
        ):
            m.fit_bounds(st.session_state["zoom_bounds"])
            st.session_state["zoom_bounds"] = None

        folium.LayerControl().add_to(m)
        map_data = st_folium(m, width=700, height=520)

        if map_data and map_data.get("last_clicked"):
            click_lat = map_data["last_clicked"]["lat"]
            click_lon = map_data["last_clicked"]["lng"]
            try:
                transformer_to_native = Transformer.from_crs(
                    "EPSG:4326", st.session_state["target_crs"], always_xy=True
                )
                native_x, native_y = transformer_to_native.transform(
                    click_lon, click_lat
                )
                if native_x != st.session_state.get(
                    "outlet_x"
                ) or native_y != st.session_state.get("outlet_y"):
                    st.session_state["outlet_x"] = native_x
                    st.session_state["outlet_y"] = native_y
                    st.rerun()
            except Exception as e:
                st.error(f"Map interaction error: {str(e)}")

    if run_delineate:
        if "dem_files_data" not in st.session_state or not st.session_state[
            "dem_files_data"
        ]:
            st.error("Please upload a DEM before running delineation.")
        else:
            with tempfile.TemporaryDirectory() as work_dir:
                target_crs = st.session_state["target_crs"]
                x_coord = st.session_state["outlet_x"]
                y_coord = st.session_state["outlet_y"]

                dem_items = st.session_state["dem_files_data"]
                merged_dem_path = os.path.join(work_dir, "input_dem.tif")

                if len(dem_items) == 1:
                    item = dem_items[0]
                    if item["is_xml"]:
                        xml_buf = io.BytesIO(item["bytes"])
                        parse_landxml_to_geotiff(xml_buf, merged_dem_path)
                    else:
                        with open(merged_dem_path, "wb") as f:
                            f.write(item["bytes"])
                else:
                    tif_paths = []
                    for idx, item in enumerate(dem_items):
                        p = os.path.join(work_dir, f"part_{idx}.tif")
                        with open(p, "wb") as f:
                            f.write(item["bytes"])
                        tif_paths.append(p)

                    src_files_to_mosaic = [
                        rasterio.open(fp) for fp in tif_paths
                    ]
                    mosaic, out_trans = merge(src_files_to_mosaic)
                    out_meta = src_files_to_mosaic[0].meta.copy()
                    out_meta.update(
                        {
                            "driver": "GTiff",
                            "height": mosaic.shape[1],
                            "width": mosaic.shape[2],
                            "transform": out_trans,
                            "crs": src_files_to_mosaic[0].crs,
                        }
                    )
                    with rasterio.open(
                        merged_dem_path, "w", **out_meta
                    ) as dest:
                        dest.write(mosaic)

                    for src in src_files_to_mosaic:
                        src.close()

                    del mosaic, src_files_to_mosaic
                    gc.collect()

                # Determine Burn-in Stream Vector Input
                burn_streams_path = None

                if stream_option == "Upload Stream Shapefile (ZIP)" and "burn_streams_bytes" in st.session_state:
                    burn_bytes = io.BytesIO(st.session_state["burn_streams_bytes"])
                    burn_dir = os.path.join(work_dir, "burn_shp")
                    os.makedirs(burn_dir, exist_ok=True)
                    with zipfile.ZipFile(burn_bytes, "r") as z:
                        z.extractall(burn_dir)
                    for root, _, files in os.walk(burn_dir):
                        for file in files:
                            if file.endswith(".shp"):
                                burn_streams_path = os.path.join(root, file)

                elif stream_option == "Use Texas Gov Dataset (ArcGIS REST)":
                    with st.spinner("Fetching NHD stream vectors from TxGIO ArcGIS REST service..."):
                        try:
                            tx_streams_gdf = fetch_texas_streams_rest(
                                target_crs, st.session_state["native_bounds"]
                            )
                            if tx_streams_gdf is not None and not tx_streams_gdf.empty:
                                st.session_state["uploaded_streams_gdf"] = tx_streams_gdf
                                burn_streams_path = os.path.join(work_dir, "tx_rest_streams.shp")
                                tx_streams_gdf.to_file(burn_streams_path)
                                st.toast(f"Retrieved {len(tx_streams_gdf)} stream segments from TxGIO.")
                            else:
                                st.warning("No Texas hydrography streams found in DEM extent.")
                        except Exception as e:
                            st.error(f"Failed to fetch Texas REST streams: {e}")

                with st.spinner(
                    "Executing WhiteboxTools hydrology workflow..."
                ):
                    wbt = get_wbt()
                    wbt.set_verbose_mode(False)

                    filled_dem = os.path.join(work_dir, "filled_dem.tif")
                    # wbt.fill_depressions(
                    #     dem=merged_dem_path, output=filled_dem, fix_flats=True
                    # )
                    
                    print("Running FillDepressionsWangAndLiu...")
                    
                    wbt.fill_depressions_wang_and_liu(
                        dem=merged_dem_path, 
                        output=filled_dem, 
                        fix_flats=True, 
                        flat_increment=None
                    )


                    dem_to_use = filled_dem

                    if burn_streams_path and os.path.exists(burn_streams_path):
                        burn_gdf = gpd.read_file(burn_streams_path)
                        if burn_gdf.crs != target_crs:
                            burn_gdf = burn_gdf.to_crs(target_crs)

                        temp_burn_shp = os.path.join(
                            work_dir, "burn_streams_reproj.shp"
                        )
                        burn_gdf.to_file(temp_burn_shp)

                        burned_dem = os.path.join(work_dir, "burned_dem.tif")
                        wbt.fill_burn(
                            dem=filled_dem,
                            streams=temp_burn_shp,
                            output=burned_dem,
                        )

                        if (
                            os.path.exists(burned_dem)
                            and os.path.getsize(burned_dem) > 0
                        ):
                            dem_to_use = burned_dem

                    d8_pointer = os.path.join(work_dir, "d8_pointer.tif")
                    wbt.d8_pointer(dem=dem_to_use, output=d8_pointer)

                    flow_acc = os.path.join(work_dir, "flow_acc.tif")
                    wbt.d8_flow_accumulation(i=dem_to_use, output=flow_acc)

                    streams_raster = os.path.join(
                        work_dir, "streams_raster.tif"
                    )
                    wbt.extract_streams(
                        flow_accum=flow_acc,
                        output=streams_raster,
                        threshold=threshold,
                    )

                    streams_vector = os.path.join(
                        work_dir, "streams_vector.shp"
                    )
                    wbt.raster_streams_to_vector(
                        streams=streams_raster,
                        d8_pntr=d8_pointer,
                        output=streams_vector,
                    )

                    outlet_gdf = gpd.GeoDataFrame(
                        geometry=[sg.Point(x_coord, y_coord)], crs=target_crs
                    )
                    pour_pts_shp = os.path.join(work_dir, "pour_point.shp")
                    outlet_gdf.to_file(pour_pts_shp)

                    snapped_pour_pts = os.path.join(
                        work_dir, "snapped_pour_points.shp"
                    )

                    try:
                        crs_obj = CRS.from_user_input(target_crs)
                        is_deg = crs_obj.is_geographic
                        wkt_str = crs_obj.to_wkt().lower()
                        is_feet = "foot" in wkt_str or "ft" in wkt_str
                    except Exception:
                        is_deg = False
                        is_feet = False

                    effective_snap_dist = float(snap_dist)
                    if is_deg:
                        conversion_factor = 364173.0 if is_feet else 111000.0
                        effective_snap_dist = (
                            float(snap_dist) / conversion_factor
                        )

                    wbt.snap_pour_points(
                        pour_pts=pour_pts_shp,
                        flow_accum=flow_acc,
                        output=snapped_pour_pts,
                        snap_dist=effective_snap_dist,
                    )

                    watershed_raster = os.path.join(
                        work_dir, "watershed_raster.tif"
                    )
                    wbt.watershed(
                        d8_pntr=d8_pointer,
                        pour_pts=snapped_pour_pts,
                        output=watershed_raster,
                    )

                    with rasterio.open(watershed_raster) as src_ws:
                        ws_data = src_ws.read(1)
                        
                        enforce_cloud_memory_limit(RAM_limit)
                        
                        ws_pixel_count = np.sum(ws_data > 0)
                        shapes_gen = rasterio.features.shapes(
                            ws_data,
                            mask=(ws_data > 0),
                            transform=src_ws.transform,
                        )
                        
                        enforce_cloud_memory_limit(RAM_limit)
                        
                        ws_geoms = [
                            sg.shape(s) for s, v in shapes_gen if v > 0
                        ]
                        del ws_data, shapes_gen
                        gc.collect()

                    if not ws_geoms:
                        st.error(
                            "❌ Delineation failed: Watershed polygon is empty. Check outlet or snap distance."
                        )
                        st.stop()

                    watershed_gdf = gpd.GeoDataFrame(
                        geometry=ws_geoms, crs=target_crs
                    )
                    st.session_state["aoi_gdf"] = watershed_gdf

                    # stream_clipped_path = os.path.join(
                    #     work_dir, "streams_clipped.shp"
                    # )
                    
                    # if os.path.exists(streams_vector):
                    #     streams_raw_gdf = gpd.read_file(
                    #         streams_vector
                    #     ).set_crs(target_crs)
                    #     stream_clipped_gdf = gpd.clip(
                    #         streams_raw_gdf, watershed_gdf
                    #     )
                    #     st.session_state["stream_gdf"] = stream_clipped_gdf
                    #     stream_clipped_gdf.to_file(stream_clipped_path)
                    
                    stream_clipped_path = os.path.join(work_dir, "streams_clipped.shp")
                    if os.path.exists(streams_vector):
                        streams_raw_gdf = gpd.read_file(streams_vector).set_crs(target_crs)
                        clipped_gdf = gpd.clip(streams_raw_gdf, watershed_gdf)
                    
                        # Filter out point/multipoint artifacts resulting from boundary intersections
                        stream_clipped_gdf = clipped_gdf[
                            clipped_gdf.geometry.type.isin(["LineString", "MultiLineString"])
                        ].copy()
                    
                        if not stream_clipped_gdf.empty:
                            st.session_state["stream_gdf"] = stream_clipped_gdf
                            stream_clipped_gdf.to_file(stream_clipped_path)
                        else:
                            st.session_state.pop("stream_gdf", None)


                    flowpath_shp = os.path.join(
                        work_dir, "longest_flowpath.shp"
                    )
                    wbt.longest_flowpath(
                        dem=dem_to_use,
                        basins=watershed_raster,
                        output=flowpath_shp,
                    )

                    longest_flow_ft = 0.0
                    longest_flow_mi = 0.0
                    
                    if os.path.exists(flowpath_shp):
                        fp_gdf = gpd.read_file(flowpath_shp).set_crs(target_crs)
                        st.session_state["flowpath_gdf"] = fp_gdf
                        
                        if not fp_gdf.empty:
                            # Reproject to local UTM if in degrees, or handle Feet vs Meters natively
                            if is_deg:
                                utm_crs = fp_gdf.estimate_utm_crs()
                                fp_proj = fp_gdf.to_crs(utm_crs)
                                length_m = fp_proj.geometry.length.sum()
                                longest_flow_ft = length_m * 3.28084
                                longest_flow_mi = length_m / 1609.34
                            elif is_feet:
                                length_ft = fp_gdf.geometry.length.sum()
                                longest_flow_ft = length_ft
                                longest_flow_mi = length_ft / 5280.0
                            else:  # Native units are meters
                                length_m = fp_gdf.geometry.length.sum()
                                longest_flow_ft = length_m * 3.28084
                                longest_flow_mi = length_m / 1609.34

                    downslope_shp = os.path.join(
                        work_dir, "downslope_flowpath.shp"
                    )
                    wbt.trace_downslope_flowpaths(
                        seed_pts=snapped_pour_pts,
                        d8_pntr=d8_pointer,
                        output=downslope_shp,
                    )
                    if os.path.exists(downslope_shp):
                        ds_gdf = gpd.read_file(downslope_shp).set_crs(
                            target_crs
                        )
                        st.session_state["downslope_gdf"] = ds_gdf

                    with rasterio.open(dem_to_use) as src_dem:
                        cell_area_native = abs(src_dem.transform[0]) * abs(
                            src_dem.transform[4]
                        )
                        try:
                            crs_obj = CRS.from_user_input(src_dem.crs)
                            wkt_str = crs_obj.to_wkt().lower()
                            is_feet = "foot" in wkt_str or "ft" in wkt_str
                        except Exception:
                            is_feet = False

                    area_native_total = ws_pixel_count * cell_area_native
                    if is_feet:
                        area_sqmi = area_native_total / 27878400.0
                        area_acres = area_native_total / 43560.0
                    else:
                        area_sqmi = area_native_total / 2589988.11
                        area_acres = area_native_total / 4046.86
                        
                        
                    

                    st.session_state["watershed_results"] = {
                        "area_sqmi": area_sqmi,
                        "area_acres": area_acres,
                        "longest_flow_mi": longest_flow_mi,
                        "longest_flow_ft": longest_flow_ft,
                    }

                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(
                        zip_buffer, "w", zipfile.ZIP_DEFLATED
                    ) as zipf:
                        layers_to_export = {
                            "watershed_boundary": watershed_gdf,
                            "streams_clipped": st.session_state.get(
                                "stream_gdf"
                            ),
                            "longest_flowpath": st.session_state.get(
                                "flowpath_gdf"
                            ),
                            "downslope_flowpath": st.session_state.get(
                                "downslope_gdf"
                            ),
                            "snapped_pour_points": (
                                gpd.read_file(snapped_pour_pts)
                                if os.path.exists(snapped_pour_pts)
                                else None
                            ), 
                            "burn_in_streams": st.session_state.get(
                                "uploaded_streams_gdf"
                            ),
                        }
                        for layer_name, gdf in layers_to_export.items():
                            if gdf is not None and not gdf.empty:
                                temp_shp = os.path.join(
                                    work_dir, f"{layer_name}.shp"
                                )
                                gdf.to_file(temp_shp)
                                for ext in [
                                    ".shp",
                                    ".shx",
                                    ".dbf",
                                    ".prj",
                                    ".cpg",
                                ]:
                                    f_p = temp_shp.replace(".shp", ext)
                                    if os.path.exists(f_p):
                                        zipf.write(
                                            f_p, arcname=f"{layer_name}{ext}"
                                        )

                    zip_buffer.seek(0)
                    st.session_state["shp_zip_bytes"] = zip_buffer.getvalue()

                st.success(
                    "🎉 Watershed delineated successfully! All temporary files cleared from disk."
                )
                gc.collect()
                st.rerun()


# -----------------------------------------------------------------------------
# TAB 2: SSURGO & NLCD CN GENERATOR
# -----------------------------------------------------------------------------

#@st.cache_data(ttl=120,  show_spinner=False)
def extract_local_nlcd_windowed(src, aoi_proj):
    xmin, ymin, xmax, ymax = aoi_proj.total_bounds
    win = src.window(xmin, ymin, xmax, ymax)
    full_canvas = Window(0, 0, src.width, src.height)
    win = win.intersection(full_canvas)

    win_trans = src.window_transform(win)
    data = src.read(1, window=win)
    height, width = data.shape

    if height == 0 or width == 0:
        raise ValueError(
            "AOI extent has zero area overlap with the local raster canvas."
        )

    geoms = [g for g in aoi_proj.geometry if g is not None and not g.is_empty]
    inside_mask = rasterio.features.geometry_mask(
        geoms,
        out_shape=(height, width),
        transform=win_trans,
        invert=True,
        all_touched=True,
    )

    valid_mask = inside_mask & (data > 0) & (data < 255)
    if not np.any(valid_mask):
        raise ValueError(
            "No valid NLCD land cover pixels found within watershed boundary."
        )

    results = (
        {"properties": {"land_use": str(int(val))}, "geometry": geom}
        for geom, val in rasterio.features.shapes(
            data, mask=valid_mask, transform=win_trans
        )
    )

    nlcd_gdf = gpd.GeoDataFrame.from_features(list(results), crs=src.crs)
    nlcd_gdf = nlcd_gdf.dissolve(by="land_use").reset_index()

    del data, inside_mask, valid_mask, results
    gc.collect()

    return nlcd_gdf

#@st.cache_data(ttl=120,  show_spinner=False)
def fetch_nlcd_dataset(aoi_gdf):
    """Downloads NLCD 2021 data dynamically via MRLC WCS based on AOI bounds."""
    aoi_5070 = aoi_gdf.to_crs("EPSG:5070")
    bounds = aoi_5070.total_bounds

    xmin, ymin = bounds[0] - 30, bounds[1] - 30
    xmax, ymax = bounds[2] + 30, bounds[3] + 30

    width = int((xmax - xmin) / 30)
    height = int((ymax - ymin) / 30)
    bbox_str = f"{xmin},{ymin},{xmax},{ymax}"

    wcs_url = (
        "https://www.mrlc.gov/geoserver/ows?version=1.1.0&SERVICE=WCS&VERSION=1.0.0&"
        "request=GetCoverage&format=GeoTIFF&coverage=mrlc_download:NLCD_2021_Land_Cover_L48&"
        f"crs=EPSG:5070&width={width}&height={height}&bbox={bbox_str}"
    )

    temp_fd, temp_path = tempfile.mkstemp(suffix=".tif")
    os.close(temp_fd)

    try:
        response = requests.get(wcs_url, stream=True, timeout=120)
        response.raise_for_status()

        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        with rasterio.open(temp_path) as src:
            nlcd_gdf = extract_local_nlcd_windowed(src, aoi_5070)
            return nlcd_gdf, "MRLC WCS NLCD 2021 Dataset"

    except Exception as e:
        raise ValueError(
            f"Failed to download or process NLCD data from MRLC: {str(e)}"
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

#@st.cache_data(ttl=120,  show_spinner=False)
def fetch_local_nlcd_2025(aoi_gdf, state_name, nlcd_year):
    """Loads NLCD land cover data from local NLCD/<year>/<state>.tif file."""
    year_str = str(nlcd_year)
    possible_paths = [
        os.path.join("NLCD", year_str, f"{state_name}.tif"),
        os.path.join("NLCD", year_str, f"{state_name.replace(' ', '_')}.tif"),
    ]

    tif_path = None
    for p in possible_paths:
        if os.path.exists(p):
            tif_path = p
            break

    if not tif_path:
        raise FileNotFoundError(
            f"Local NLCD {nlcd_year} raster file not found for '{state_name}'. Expected at: {possible_paths[0]}"
        )

    with rasterio.open(tif_path) as src:
        aoi_proj = aoi_gdf.to_crs(src.crs)
        nlcd_gdf = extract_local_nlcd_windowed(src, aoi_proj)
        return nlcd_gdf, f"Local NLCD {nlcd_year} Dataset ({state_name})"

#@st.cache_data(ttl=120,  show_spinner=False)
def download_ssurgo_extended(aoi_gdf):
    """Downloads SSURGO data via WFS directly into RAM buffer."""
    aoi_4326 = aoi_gdf.to_crs("EPSG:4326")
    bounds = aoi_4326.total_bounds
    bbox_str = f"{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}"

    wfs_url = (
        "https://sdmdataaccess.sc.egov.usda.gov/Spatial/SDMWGS84GEOGRAPHIC.wfs".strip()
    )
    params = {
        "SERVICE": "WFS",
        "VERSION": "1.1.0",
        "REQUEST": "GetFeature",
        "TYPENAME": "mapunitpolyextended",
        "SRSNAME": "EPSG:4326",
        "BBOX": bbox_str,
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }

    response = requests.get(
        wfs_url, params=params, headers=headers, timeout=60
    )
    response.raise_for_status()

    gml_bytes = io.BytesIO(response.content)
    ssurgo_gdf = gpd.read_file(gml_bytes)

    if ssurgo_gdf.empty:
        return ssurgo_gdf

    ssurgo_gdf.geometry = ssurgo_gdf.geometry.map(
        lambda geom: (
            shapely.ops.transform(lambda x, y: (y, x), geom) if geom else None
        )
    )
    ssurgo_gdf.set_crs("EPSG:4326", inplace=True)
    return ssurgo_gdf


def clean_hsg(val):
    if pd.isna(val) or val is None:
        return ""
    val_str = str(val).strip().upper()
    if val_str in ["NONE", "NULL", "NAN", "", "0"]:
        return ""
    if "/" in val_str:
        parts = [p.strip() for p in val_str.split("/") if p.strip()]
        return parts[-1] if parts else ""
    return val_str

#@st.cache_data(ttl=120,  show_spinner=False)
def calculate_weighted_cn(aoi_gdf, nlcd_gdf, ssurgo_gdf, lookup_csv_path):
    target_crs = (
        aoi_gdf.crs
        if (aoi_gdf.crs and aoi_gdf.crs.is_projected)
        else "EPSG:5070"
    )

    aoi_proj = aoi_gdf.to_crs(target_crs)
    nlcd_proj = nlcd_gdf.to_crs(target_crs)
    ssurgo_proj = ssurgo_gdf.to_crs(target_crs)

    ssurgo_clipped = gpd.clip(ssurgo_proj, aoi_proj)
    nlcd_clipped = gpd.clip(nlcd_proj, aoi_proj)

    if ssurgo_clipped.empty or nlcd_clipped.empty:
        raise ValueError(
            "SSURGO or NLCD dataset returned empty geometry when clipped."
        )

    hyg_col = next(
        (
            c
            for c in ssurgo_clipped.columns
            if c.lower() in ["hydgrpdcd", "hydgrp", "hyg"]
        ),
        None,
    )
    ssurgo_clipped["hyg_clean"] = (
        ssurgo_clipped[hyg_col].apply(clean_hsg) if hyg_col else ""
    )
    nlcd_clipped["land_use_clean"] = (
        nlcd_clipped["land_use"].astype(str).str.strip().str.split(".").str[0]
    )

    ssurgo_grouped = (
        ssurgo_clipped[["hyg_clean", "geometry"]]
        .dissolve(by="hyg_clean")
        .reset_index()
    )
    nlcd_grouped = (
        nlcd_clipped[["land_use_clean", "geometry"]]
        .dissolve(by="land_use_clean")
        .reset_index()
    )
    
    enforce_cloud_memory_limit(RAM_limit)

    final_intersect = gpd.overlay(
        ssurgo_grouped, nlcd_grouped, how="intersection"
    )
    
    enforce_cloud_memory_limit(RAM_limit)

    if final_intersect.empty:
        raise ValueError("Spatial intersection yielded empty geometry.")

    final_intersect["grid_code"] = (
        final_intersect["land_use_clean"] + "_" + final_intersect["hyg_clean"]
    )

    lookup_df = pd.read_csv(lookup_csv_path)
    lookup_df.columns = [c.strip().lower() for c in lookup_df.columns]

    lookup_df["grid_code"] = lookup_df["grid_code"].astype(str).str.strip()
    lookup_df["cn"] = pd.to_numeric(lookup_df["cn"], errors="coerce")

    merged = final_intersect.merge(lookup_df, on="grid_code", how="left")

    merged["area_sqm"] = merged.geometry.area
    total_area = merged["area_sqm"].sum()

    if total_area == 0:
        raise ValueError("Total area of intersected polygon features is zero.")

    merged["cn"] = merged["cn"].fillna(0)
    merged["area_x_cn"] = merged["area_sqm"] * merged["cn"]

    weighted_cn = merged["area_x_cn"].sum() / total_area

    del (
        ssurgo_clipped,
        nlcd_clipped,
        ssurgo_grouped,
        nlcd_grouped,
        final_intersect,
    )
    gc.collect()

    return weighted_cn, merged


def calculate_weighted_cn_and_c(
    aoi_gdf, nlcd_gdf, ssurgo_gdf, lookup_csv_path
):
    target_crs = (
        aoi_gdf.crs
        if (aoi_gdf.crs and aoi_gdf.crs.is_projected)
        else "EPSG:5070"
    )

    aoi_proj = aoi_gdf.to_crs(target_crs)
    nlcd_proj = nlcd_gdf.to_crs(target_crs)
    ssurgo_proj = ssurgo_gdf.to_crs(target_crs)

    ssurgo_clipped = gpd.clip(ssurgo_proj, aoi_proj)
    nlcd_clipped = gpd.clip(nlcd_proj, aoi_proj)

    if ssurgo_clipped.empty or nlcd_clipped.empty:
        raise ValueError(
            "SSURGO or NLCD dataset returned empty geometry when clipped."
        )

    hyg_col = next(
        (
            c
            for c in ssurgo_clipped.columns
            if c.lower() in ["hydgrpdcd", "hydgrp", "hyg"]
        ),
        None,
    )
    ssurgo_clipped["hyg_clean"] = (
        ssurgo_clipped[hyg_col].apply(clean_hsg) if hyg_col else ""
    )
    nlcd_clipped["land_use_clean"] = (
        nlcd_clipped["land_use"].astype(str).str.strip().str.split(".").str[0]
    )

    ssurgo_grouped = (
        ssurgo_clipped[["hyg_clean", "geometry"]]
        .dissolve(by="hyg_clean")
        .reset_index()
    )
    nlcd_grouped = (
        nlcd_clipped[["land_use_clean", "geometry"]]
        .dissolve(by="land_use_clean")
        .reset_index()
    )

    enforce_cloud_memory_limit(RAM_limit)

    final_intersect = gpd.overlay(
        ssurgo_grouped, nlcd_grouped, how="intersection"
    )

    enforce_cloud_memory_limit(RAM_limit)

    if final_intersect.empty:
        raise ValueError("Spatial intersection yielded empty geometry.")

    final_intersect["grid_code"] = (
        final_intersect["land_use_clean"] + "_" + final_intersect["hyg_clean"]
    )

    lookup_df = pd.read_csv(lookup_csv_path)
    lookup_df.columns = [c.strip().lower() for c in lookup_df.columns]

    lookup_df["grid_code"] = lookup_df["grid_code"].astype(str).str.strip()

    # Parse both CN and C columns
    lookup_df["cn"] = pd.to_numeric(lookup_df["cn"], errors="coerce")
    lookup_df["c"] = pd.to_numeric(
        lookup_df["c"], errors="coerce"
    )  # Reads column 'C'

    merged = final_intersect.merge(lookup_df, on="grid_code", how="left")

    merged["area_sqm"] = merged.geometry.area
    total_area = merged["area_sqm"].sum()

    if total_area == 0:
        raise ValueError("Total area of intersected polygon features is zero.")

    # Calculate Weighted Curve Number (CN)
    merged["cn"] = merged["cn"].fillna(0)
    merged["area_x_cn"] = merged["area_sqm"] * merged["cn"]
    weighted_cn = merged["area_x_cn"].sum() / total_area

    # Calculate Weighted Runoff Coefficient (C)
    merged["c"] = merged["c"].fillna(0)
    merged["area_x_c"] = merged["area_sqm"] * merged["c"]
    weighted_c = merged["area_x_c"].sum() / total_area

    del (
        ssurgo_clipped,
        nlcd_clipped,
        ssurgo_grouped,
        nlcd_grouped,
        final_intersect,
    )
    gc.collect()

    return weighted_cn, weighted_c, merged



NLCD_COLOR_MAP = {
    "11": ("#466B9F", "Open Water"),
    "12": ("#D1DEF8", "Perennial Ice/Snow"),
    "21": ("#DEC5C5", "Developed, Open Space"),
    "22": ("#D99282", "Developed, Low Intensity"),
    "23": ("#EB0000", "Developed, Medium Intensity"),
    "24": ("#AB0000", "Developed, High Intensity"),
    "31": ("#B3AC9F", "Barren Land"),
    "41": ("#68AB5F", "Deciduous Forest"),
    "42": ("#1C5F2C", "Evergreen Forest"),
    "43": ("#B5C58F", "Mixed Forest"),
    "52": ("#CCBAA4", "Shrub/Scrub"),
    "71": ("#E2E2C1", "Grassland/Herbaceous"),
    "81": ("#DBD83D", "Pasture/Hay"),
    "82": ("#AA7028", "Cultivated Crops"),
    "90": ("#BAD8EA", "Woody Wetlands"),
    "95": ("#70A3BA", "Emergent Herbaceous Wetlands"),
}

SOIL_COLOR_MAP = {
    "A": ("#2CA02C", "Group A (High Infiltration)"),
    "B": ("#1F77B4", "Group B (Moderate Infiltration)"),
    "C": ("#FF7F0E", "Group C (Slow Infiltration)"),
    "D": ("#D62728", "Group D (Very Slow Infiltration)"),
    "": ("#7F7F7F", "Unclassified / None"),
}


def get_cn_color(val):
    try:
        cn = float(val)
    except (ValueError, TypeError):
        return "#7F7F7F"
    if cn < 40:
        return "#1A9850"
    elif cn < 55:
        return "#91CF60"
    elif cn < 70:
        return "#D9EF8B"
    elif cn < 80:
        return "#FEE08B"
    elif cn < 90:
        return "#FC8D59"
    else:
        return "#D73027"


def bundle_cn_project_zip_in_memory(aoi_gdf, ssurgo_gdf, nlcd_gdf, cn_gdf):
    """Generates Shapefile Package inside an in-memory BytesIO stream."""
    mem_zip = io.BytesIO()
    layers = {
        "watershed_boundary": aoi_gdf,
        "ssurgo_soils": ssurgo_gdf,
        "nlcd_landuse": nlcd_gdf,
        "curve_number_polygons": cn_gdf,
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(mem_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            for layer_name, gdf in layers.items():
                if gdf is not None and not gdf.empty:
                    shp_path = os.path.join(temp_dir, f"{layer_name}.shp")
                    gdf.copy().to_file(shp_path)

                    for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
                        f_path = shp_path.replace(".shp", ext)
                        if os.path.exists(f_path):
                            zipf.write(f_path, arcname=f"{layer_name}{ext}")

    mem_zip.seek(0)
    return mem_zip.getvalue()


with tab2:
    st.header("Automated SSURGO & NLCD Curve Number Generator")
    st.markdown(
        "Download remote datasets, view categorized maps with official legends, and extract composite Curve Numbers."
    )

    input_source = st.radio(
        "Select Drainage Area Input:",
        [
            "Use Watershed from Watershed Delineation Tab",
            "Upload Zipped Shapefile",
        ],
    )

    if input_source == "Use Watershed from Watershed Delineation Tab":
        if (
            "aoi_gdf" in st.session_state
            and st.session_state["aoi_gdf"] is not None
        ):
            if (
                st.session_state.get("cn_aoi_gdf")
                is not st.session_state["aoi_gdf"]
            ):
                st.session_state["cn_aoi_gdf"] = st.session_state["aoi_gdf"]
                b = (
                    st.session_state["cn_aoi_gdf"]
                    .to_crs("EPSG:4326")
                    .total_bounds
                )
                st.session_state["cn_zoom_bounds"] = [
                    [b[1], b[0]],
                    [b[3], b[2]],
                ]
        else:
            st.warning("No watershed delineated in Tab 1 yet.")
            st.session_state.pop("cn_aoi_gdf", None)
    elif input_source == "Upload Zipped Shapefile":
        zip_upload = st.file_uploader(
            "Upload Drainage Area (ZIP containing .shp)", type=["zip"]
        )
        if zip_upload:
            with tempfile.TemporaryDirectory() as temp_dir:
                shp_path = extract_uploaded_archive_in_temp(
                    zip_upload, temp_dir
                )
                if shp_path:
                    uploaded_gdf = gpd.read_file(shp_path)
                    st.session_state["cn_aoi_gdf"] = uploaded_gdf
                    b = uploaded_gdf.to_crs("EPSG:4326").total_bounds
                    st.session_state["cn_zoom_bounds"] = [
                        [b[1], b[0]],
                        [b[3], b[2]],
                    ]
                else:
                    st.error("No valid .shp file found in the ZIP.")

    st.markdown("---")
    st.subheader("NLCD Data Settings")
    nlcd_year = st.selectbox(
        "Select NLCD Year:", [2021, 2017, 2025], index=0, key="nlcd_year_select"
    )

    selected_state = None
    if nlcd_year in (2017, 2025):
        selected_state = st.selectbox(
            "Select State for Drainage Area:",
            US_STATES,
            key="nlcd_state_select",
        )

    has_aoi = "cn_aoi_gdf" in st.session_state
    has_nlcd = "cn_nlcd_gdf" in st.session_state
    has_ssurgo = "cn_ssurgo_gdf" in st.session_state
    has_cn = "cn_intersected_gdf" in st.session_state

    st.markdown("---")
    st.subheader("User Curve Number Lookup")

    lookup_col1, lookup_col2 = st.columns([2, 1])
    with lookup_col1:
        lookup_upload = st.file_uploader(
            "Upload Custom NLCD-HSG CN Lookup Table (.csv)",
            type=["csv"],  key="lookup_upload_file",
            help="Optional. If not uploaded, the default root folder table will be used.",
        )
        

    with lookup_col2:
        default_lookup_path = "NLCD_SHG_CN_lookup.csv"
        st.write("")
        st.write("")
        if os.path.exists(default_lookup_path):
            with open(default_lookup_path, "rb") as f:
                st.download_button(
                    label="📄 Download Sample Lookup Table",
                    data=f,
                    file_name="Sample_NLCD_SHG_CN_lookup.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
        else:
            st.info("Sample table not found in root directory.")

    st.markdown("---")
    col_btn1, col_btn2, col_btn3 = st.columns(3)

    with col_btn1:
        if st.button(
            "📥 1. Download NLCD",
            disabled=not has_aoi,
            use_container_width=True,
        ):
            with st.spinner(f"Processing NLCD {nlcd_year} Land Cover dataset..."):
                try:
                    if nlcd_year in (2017, 2025):
                        if not selected_state:
                            st.error(
                                "Please select a state for NLCD {nlcd_year} dataset."
                            )
                        else:
                            nlcd_gdf, source_ver = fetch_local_nlcd_2025(
                                st.session_state["cn_aoi_gdf"], selected_state, nlcd_year,
                            )
                            st.session_state["cn_nlcd_gdf"] = nlcd_gdf
                            st.session_state["nlcd_source_version"] = source_ver
                            gc.collect()
                            st.rerun()
                    else:
                        nlcd_gdf, source_ver = fetch_nlcd_dataset(
                            st.session_state["cn_aoi_gdf"]
                        )
                        st.session_state["cn_nlcd_gdf"] = nlcd_gdf
                        st.session_state["nlcd_source_version"] = source_ver
                        gc.collect()
                        st.rerun()
                except Exception as e:
                    st.error(str(e))

    with col_btn2:
        if st.button(
            "📥 2. Download SSURGO",
            disabled=not has_aoi,
            use_container_width=True,
        ):
            with st.spinner("Downloading SSURGO Soils via USDA WFS in RAM..."):
                try:
                    st.session_state["cn_ssurgo_gdf"] = (
                        download_ssurgo_extended(st.session_state["cn_aoi_gdf"])
                    )
                    gc.collect()
                    st.rerun()
                except Exception as e:
                    st.error(f"SSURGO Download Failed: {e}")

    with col_btn3:
        if st.button(
            "📊 3. Calculate CN",
            disabled=not (has_aoi and has_nlcd and has_ssurgo),
            type="primary",
            use_container_width=True,
        ):
            with st.spinner(
                "Intersecting layers and calculating weighted CN..."
            ):
                temp_lookup_path = None
                try:
                    if lookup_upload is not None:
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".csv"
                        ) as tmp:
                            tmp.write(lookup_upload.getvalue())
                            temp_lookup_path = tmp.name
                        active_lookup_path = temp_lookup_path
                    else:
                        active_lookup_path = "NLCD_SHG_CN_lookup.csv"

                    if not os.path.exists(active_lookup_path):
                        st.error(
                            f"Lookup table '{active_lookup_path}' not found. Please upload one or ensure it exists in the app root."
                        )
                    else:
                        weighted_cn,weighted_c, intersected_gdf = calculate_weighted_cn_and_c(
                            st.session_state["cn_aoi_gdf"],
                            st.session_state["cn_nlcd_gdf"],
                            st.session_state["cn_ssurgo_gdf"],
                            active_lookup_path,
                        )
                        st.session_state["final_cn"] = weighted_cn
                        st.session_state["final_c"] = weighted_c
                        st.session_state["cn_intersected_gdf"] = (
                            intersected_gdf
                        )

                        st.session_state["cn_zip_bytes"] = (
                            bundle_cn_project_zip_in_memory(
                                st.session_state["cn_aoi_gdf"],
                                st.session_state["cn_ssurgo_gdf"],
                                st.session_state["cn_nlcd_gdf"],
                                intersected_gdf,
                            )
                        )
                        gc.collect()
                        st.rerun()
                except Exception as e:
                    st.error(f"Calculation Error: {e}")
                finally:
                    if temp_lookup_path and os.path.exists(temp_lookup_path):
                        os.remove(temp_lookup_path)

    if "nlcd_source_version" in st.session_state:
        st.info(
            f"ℹ️ **NLCD Data Source Used:** {st.session_state['nlcd_source_version']}"
        )

    if "final_cn" in st.session_state:
        st.markdown("---")
        intersected_gdf = st.session_state["cn_intersected_gdf"]
        crs_obj = intersected_gdf.crs
        
        # 1. Fallback to EPSG:5070 (Equal Area, Meters) if CRS is missing or Geographic
        if crs_obj is None or crs_obj.is_geographic:
            calc_gdf = intersected_gdf.to_crs("EPSG:5070")
            crs_obj = calc_gdf.crs
        else:
            calc_gdf = intersected_gdf
        
        # 2. Inspect CRS linear unit name
        unit_name = ""
        if crs_obj and crs_obj.axis_info:
            unit_name = str(crs_obj.axis_info[0].unit_name).lower()
        
        raw_area = calc_gdf.geometry.area.sum()
        
        # 3. Calculate area based on detected CRS units
        if "foot" in unit_name or "ft" in unit_name or "feet" in unit_name:
            total_area_sqft = raw_area
            total_area_sqmi = total_area_sqft / 27_878_400
            total_area_sqm = total_area_sqft / 10.7639104167
            total_area_acres = total_area_sqft / 43_560
        else:
            # Standard metric projected CRS (meters)
            total_area_sqm = raw_area
            total_area_sqft = total_area_sqm * 10.7639104167
            total_area_sqmi = total_area_sqft / 27_878_400
            total_area_acres = total_area_sqft / 43_560
        
        # 4. Display Metrics
        # col_res1, col_res2 = st.columns([3, 1])
        
        # with col_res1:
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        with m_col1:
            st.metric(
                label="Weighted CN",
                value=f"{st.session_state['final_cn']:.2f}",
            )
        with m_col2:
            st.metric(
                label="Weighted C",
                value=f"{st.session_state['final_c']:.2f}",
            )
        with m_col3:
            st.metric(
                label="Total Area (mi²)",
                value=f"{total_area_sqmi:.3f}",
            )
        with m_col4:
            st.metric(
                label="Total Area (Acres)",
                value=f"{total_area_acres:,.0f}",
            )
                
        st.divider()
        #with col_res2:
        st.subheader("📋 CN Breakdown Table")
        
        intersected_gdf = st.session_state["cn_intersected_gdf"]
        
        target_crs = intersected_gdf.crs
        if target_crs is None or target_crs.is_geographic:
            calc_gdf = intersected_gdf.to_crs("EPSG:5070")
        else:
            calc_gdf = intersected_gdf.copy()

        crs_wkt = calc_gdf.crs.to_wkt().lower()
        is_feet = "foot" in crs_wkt or "ft" in crs_wkt
        
        calc_gdf["area_sqm"] = calc_gdf.geometry.area
        if is_feet:
            calc_gdf["area_acres"] = calc_gdf["area_sqm"] / 43560.0
        else:
            calc_gdf["area_acres"] = calc_gdf["area_sqm"] / 4046.8564224

        total_acres = calc_gdf["area_acres"].sum()

        
        
        summary_df = calc_gdf.copy()
        summary_df["% Area"] = (summary_df["area_acres"] / total_area_acres * 100).round(2)
        summary_df["Area (Acres)"] = summary_df["area_acres"].round(2)
        
        rename_dict = {
            "land_use_clean": "Land Use Code",
            "hyg_clean": "Soil HSG",
            "cn": "Curve Number (CN)",
            "c": "Runoff Coeff (C)",
            "grid_code": "Grid Code"
            }
        summary_renamed = summary_df.rename(columns=rename_dict)
        table_cols = [c for c in ["Land Use Code", "Soil HSG", "Grid Code", "Curve Number (CN)", "Runoff Coeff (C)", "Area (Acres)", "% Area"] if c in summary_renamed.columns]
        
        formatted_df = summary_renamed[table_cols]
        st.dataframe(formatted_df, use_container_width=True)

        csv_bytes = formatted_df.to_csv(index=False).encode("utf-8")
        
        col_dl1, col_dl2 = st.columns(2)
        with col_dl1:
            st.download_button(
                label="📊 Download CN Breakdown Summary (CSV)",
                data=csv_bytes,
                file_name="cn_breakdown_summary.csv",
                mime="text/csv",
                use_container_width=True
            )

        with col_dl2:
            if "cn_zip_bytes" in st.session_state:
                st.download_button(
                    label="📦 Download Complete CN GIS Package (Watershed, SSURGO, NLCD & CN Shapefiles)",
                    data=st.session_state["cn_zip_bytes"],
                    file_name="cn_hydrology_project_all_shapefiles.zip",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                )


                
                
        # with col_res2:
        #     if "cn_zip_bytes" in st.session_state:
        #         st.download_button(
        #             label="📦 Download Complete CN GIS Package (Watershed, SSURGO, NLCD & CN Shapefiles)",
        #             data=st.session_state["cn_zip_bytes"],
        #             file_name="cn_hydrology_project_all_shapefiles.zip",
        #             mime="application/zip",
        #             use_container_width=True,
        #             type="primary",
        #         )

    st.markdown("---")
    col_map_head, col_zoom_btn = st.columns([3, 1])
    with col_map_head:
        st.subheader("Interactive Map Viewer")
    with col_zoom_btn:
        zoom_cn_click = st.button(
            "🔍 Zoom to Drainage Area",
            disabled=not has_aoi,
            use_container_width=True,
        )
        if zoom_cn_click and has_aoi:
            b = st.session_state["cn_aoi_gdf"].to_crs("EPSG:4326").total_bounds
            st.session_state["cn_zoom_bounds"] = [[b[1], b[0]], [b[3], b[2]]]

    col_map_view, col_legends = st.columns([3, 1])

    with col_map_view:
        start_loc = [30.2672, -97.7431]
        if has_aoi:
            bounds = (
                st.session_state["cn_aoi_gdf"].to_crs("EPSG:4326").total_bounds
            )
            start_loc = [
                (bounds[1] + bounds[3]) / 2,
                (bounds[0] + bounds[2]) / 2,
            ]

        m2 = folium.Map(
            location=start_loc, zoom_start=13, tiles="OpenStreetMap"
        )

        if has_nlcd:
            nlcd_4326 = (
                st.session_state["cn_nlcd_gdf"].to_crs("EPSG:4326").copy()
            )
            nlcd_4326["code_str"] = (
                nlcd_4326["land_use"]
                .astype(str)
                .str.strip()
                .str.split(".")
                .str[0]
            )

            folium.GeoJson(
                nlcd_4326,
                name="NLCD Land Cover (Official Colors)",
                style_function=lambda feature: {
                    "fillColor": NLCD_COLOR_MAP.get(
                        feature["properties"].get("code_str", ""),
                        ("#7F7F7F", ""),
                    )[0],
                    "color": "#555555",
                    "weight": 0.5,
                    "fillOpacity": 0.6,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["code_str"], aliases=["NLCD Land Cover Code:"]
                ),
            ).add_to(m2)

        if has_ssurgo:
            ssurgo_4326 = (
                st.session_state["cn_ssurgo_gdf"].to_crs("EPSG:4326").copy()
            )
            hyg_col = next(
                (
                    c
                    for c in ssurgo_4326.columns
                    if c.lower() in ["hydgrpdcd", "hydgrp", "hyg"]
                ),
                None,
            )
            ssurgo_4326["style_hsg"] = (
                ssurgo_4326[hyg_col].apply(clean_hsg) if hyg_col else ""
            )

            folium.GeoJson(
                ssurgo_4326,
                name="SSURGO Soil Groups",
                style_function=lambda feature: {
                    "fillColor": SOIL_COLOR_MAP.get(
                        feature["properties"].get("style_hsg", ""),
                        ("#7F7F7F", ""),
                    )[0],
                    "color": "#333333",
                    "weight": 1,
                    "fillOpacity": 0.5,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=[hyg_col] if hyg_col else ["style_hsg"],
                    aliases=["Hydrologic Soil Group:"],
                ),
            ).add_to(m2)

        if has_aoi:
            aoi_4326 = st.session_state["cn_aoi_gdf"].to_crs("EPSG:4326")
            folium.GeoJson(
                aoi_4326,
                name="Watershed Boundary",
                style_function=lambda x: {
                    "color": "red",
                    "fillOpacity": 0,
                    "weight": 3.5,
                },
            ).add_to(m2)

        if (
            "cn_zoom_bounds" in st.session_state
            and st.session_state["cn_zoom_bounds"] is not None
        ):
            m2.fit_bounds(st.session_state["cn_zoom_bounds"])
            st.session_state["cn_zoom_bounds"] = None

        if has_cn:
            cn_4326 = (
                st.session_state["cn_intersected_gdf"]
                .to_crs("EPSG:4326")
                .copy()
            )

            folium.GeoJson(
                cn_4326,
                name="Calculated CN Polygons",
                style_function=lambda feature: {
                    "fillColor": get_cn_color(
                        feature["properties"].get("cn", 0)
                    ),
                    "color": "#000000",
                    "weight": 1,
                    "fillOpacity": 0.7,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["grid_code", "cn", "land_use_clean", "hyg_clean", "c"],
                    aliases=[
                        "Grid Code:",
                        "Curve Number (CN):",
                        "NLCD Code:",
                        "Soil HSG:",
                        "Runoff Coefficient (C) :",
                    ],
                ),
            ).add_to(m2)

        folium.LayerControl().add_to(m2)
        st_folium(m2, width=800, height=520, returned_objects=[])

    with col_legends:
        st.subheader("🗺️ Map Legends")

        if has_cn:
            with st.expander("📊 Curve Number Ramp", expanded=True):
                st.markdown(
                    """
                <div style="font-size:12px;">
                <span style="color:#1A9850;">■</span> &lt; 40 (Low Runoff)<br>
                <span style="color:#91CF60;">■</span> 40 - 55<br>
                <span style="color:#D9EF8B;">■</span> 55 - 70<br>
                <span style="color:#FEE08B;">■</span> 70 - 80<br>
                <span style="color:#FC8D59;">■</span> 80 - 90<br>
                <span style="color:#D73027;">■</span> &gt; 90 (High Runoff)
                </div>
                """,
                    unsafe_allow_html=True,
                )

        if has_ssurgo:
            with st.expander("🌱 Soil HSG Legend", expanded=True):
                for grp, (hex_c, label) in SOIL_COLOR_MAP.items():
                    key_name = grp if grp else "None"
                    st.markdown(
                        f'<span style="color:{hex_c};font-size:16px;">■</span> **{key_name}**: <span style="font-size:11px;">{label}</span>',
                        unsafe_allow_html=True,
                    )

        if has_nlcd:
            with st.expander("🌲 NLCD Land Cover Legend", expanded=True):
                present_codes = set(
                    st.session_state["cn_nlcd_gdf"]["land_use"]
                    .astype(str)
                    .str.strip()
                    .str.split(".")
                    .str[0]
                )
                for code, (hex_c, desc) in NLCD_COLOR_MAP.items():
                    if code in present_codes or not present_codes:
                        st.markdown(
                            f'<span style="color:{hex_c};font-size:16px;">■</span> **{code}**: <span style="font-size:11px;">{desc}</span>',
                            unsafe_allow_html=True,
                        )