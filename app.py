import base64
import io
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile

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

import gc

st.set_page_config(
    page_title="Hydrology & CN Web Suite",
    page_icon="🌊",
    layout="wide",
)


# -----------------------------------------------------------------------------
# HELPER & IN-MEMORY RASTER FUNCTIONS
# -----------------------------------------------------------------------------


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

    col_in, col_map = st.columns([1, 2])

    with col_in:
        st.subheader("1. Elevation & Hydro Inputs")
        terrain_files = st.file_uploader(
            "Upload DEM (GeoTIFF or LandXML)",
            type=["tif", "tiff", "xml"],
            accept_multiple_files=True,
        )

        stream_burn_file = st.file_uploader(
            "Upload Streams for DEM Burning (Optional ZIP Shapefile)",
            type=["zip"],
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

        # Multi-file validation & DEM loading logic
        if terrain_files:
            total_size_mb = sum(f.size for f in terrain_files) / (1024 * 1024)
            if total_size_mb > 150:
                st.error(
                    f"❌ Total combined file size ({total_size_mb:.1f} MB) exceeds the 150 MB limit."
                )
                st.stop()

            is_multi = len(terrain_files) > 1
            has_xml = any(f.name.lower().endswith(".xml") for f in terrain_files)

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

                # Process Rasters in RAM for Multi-Overlay & Bounds Calculation
                processed_dems = []
                global_min = float("inf")
                global_max = float("-inf")
                all_map_bounds = []

                primary_crs = None
                native_min_x, native_min_y = float("inf"), float("inf")
                native_max_x, native_max_y = float("-inf"), float("-inf")

                with st.spinner("Processing elevation surfaces and map overlay..."):
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

                                native_min_x = min(native_min_x, src.bounds.left)
                                native_max_x = max(native_max_x, src.bounds.right)
                                native_min_y = min(native_min_y, src.bounds.bottom)
                                native_max_y = max(native_max_y, src.bounds.top)

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

                                reproject(
                                    source=rasterio.band(src, 1),
                                    destination=destination,
                                    src_transform=src.transform,
                                    src_crs=src.crs,
                                    dst_transform=scaled_transform,
                                    dst_crs=dst_crs,
                                    resampling=Resampling.average,
                                )

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
                st.session_state["outlet_x"] = (
                    native_min_x + native_max_x
                ) / 2.0
                st.session_state["outlet_y"] = (
                    native_min_y + native_max_y
                ) / 2.0
                st.session_state.pop("stream_gdf", None)
                st.session_state.pop("downslope_gdf", None)
                st.session_state.pop("watershed_results", None)

        # if stream_burn_file is not None:
        #     st.session_state["burn_streams_bytes"] = stream_burn_file.getbuffer()
        # Replace lines 274-275 with:
        if stream_burn_file is not None:
            st.session_state["burn_streams_bytes"] = stream_burn_file.getbuffer()
            try:
                # Ephemeral extraction to load shapefile into RAM
                with tempfile.TemporaryDirectory() as temp_dir:
                    zip_bytes = io.BytesIO(stream_burn_file.getvalue())
                    with zipfile.ZipFile(zip_bytes, "r") as z:
                        z.extractall(temp_dir)

                    # Search for .shp file (handles nested subfolders inside ZIP)
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
        else:
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
            zoom_click = st.button("🔍 Zoom to Terrain", use_container_width=True)
            if zoom_click and "map_bounds" in st.session_state:
                st.session_state["zoom_bounds"] = st.session_state["map_bounds"]

        with col_btn2:
            run_delineate = st.button(
                "⛰️ Run WBT Delineation",
                type="primary",
                use_container_width=True,
            )

        # RESULTS & DOWNLOAD PORTAL
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

        # Outline styling for map headers
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

        # Build shared Colormap and ImageOverlays for DEMs
        if "processed_dems" in st.session_state:
            p_dems = st.session_state["processed_dems"]
            g_min = st.session_state["global_min"]
            g_max = st.session_state["global_max"]

            terrain_cmap = cm.get_cmap("terrain", 15)
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
                        colored = cm.terrain(norm / 255.0) * 255
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
            
        # Display Uploaded Stream Lines for DEM Burning
        if "uploaded_streams_gdf" in st.session_state:
            try:
                uploaded_4326 = st.session_state["uploaded_streams_gdf"].to_crs("EPSG:4326")
                folium.GeoJson(
                    uploaded_4326,
                    name="Uploaded Burn-in Streams",
                    style_function=lambda x: {
                        "color": "#00FFFF",  # Cyan highlight
                        "weight": 3,
                        "opacity": 0.9,
                        "dashArray": "4, 4"
                    },
                    tooltip="Uploaded Stream Line (Burn-in Channel)"
                ).add_to(m)
            except Exception as e:
                st.warning(f"Could not render uploaded streams on map: {e}")

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

    # -------------------------------------------------------------------------
    # WHITEBOXTOOLS PIPELINE (SCOPED TEMP DIR FOR AUTO-CLEANUP)
    # -------------------------------------------------------------------------
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
                    # Merge multiple GeoTIFF files into a single continuous surface
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

                burn_streams_path = None
                if "burn_streams_bytes" in st.session_state:
                    burn_bytes = io.BytesIO(
                        st.session_state["burn_streams_bytes"]
                    )
                    burn_dir = os.path.join(work_dir, "burn_shp")
                    os.makedirs(burn_dir, exist_ok=True)
                    with zipfile.ZipFile(burn_bytes, "r") as z:
                        z.extractall(burn_dir)
                    for root, _, files in os.walk(burn_dir):
                        for file in files:
                            if file.endswith(".shp"):
                                burn_streams_path = os.path.join(root, file)

                with st.spinner("Executing WhiteboxTools hydrology workflow..."):
                    wbt = whitebox.WhiteboxTools()
                    wbt.set_verbose_mode(False)

                    # # Step 1: Fill Depressions
                    # filled_dem = os.path.join(work_dir, "filled_dem.tif")
                    # wbt.fill_depressions(
                    #     dem=merged_dem_path, output=filled_dem, fix_flats=True
                    # )

                    # dem_to_use = filled_dem
                    # if burn_streams_path and os.path.exists(burn_streams_path):
                    #     burned_dem = os.path.join(work_dir, "burned_dem.tif")
                    #     wbt.fill_burn(
                    #         dem=filled_dem,
                    #         streams=burn_streams_path,
                    #         output=burned_dem,
                    #     )
                    #     dem_to_use = burned_dem
                    
                    # Step 1: Fill Depressions
                    filled_dem = os.path.join(work_dir, "filled_dem.tif")
                    wbt.fill_depressions(
                        dem=merged_dem_path, output=filled_dem, fix_flats=True
                    )

                    dem_to_use = filled_dem

                    # Step 1b: Reproject and Burn Uploaded Streams
                    if "uploaded_streams_gdf" in st.session_state:
                        burn_gdf = st.session_state["uploaded_streams_gdf"].copy()
                        
                        # Ensure Stream CRS matches DEM Target CRS
                        if burn_gdf.crs != target_crs:
                            burn_gdf = burn_gdf.to_crs(target_crs)

                        temp_burn_shp = os.path.join(work_dir, "burn_streams_reproj.shp")
                        burn_gdf.to_file(temp_burn_shp)

                        burned_dem = os.path.join(work_dir, "burned_dem.tif")
                        wbt.fill_burn(
                            dem=filled_dem,
                            streams=temp_burn_shp,
                            output=burned_dem,
                        )

                        if os.path.exists(burned_dem) and os.path.getsize(burned_dem) > 0:
                            dem_to_use = burned_dem
                            
                            
                    # Step 2: D8 Pointer & Flow Accumulation
                    d8_pointer = os.path.join(work_dir, "d8_pointer.tif")
                    wbt.d8_pointer(dem=dem_to_use, output=d8_pointer)

                    flow_acc = os.path.join(work_dir, "flow_acc.tif")
                    wbt.d8_flow_accumulation(i=dem_to_use, output=flow_acc)

                    # Step 3: Extract Streams
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

                    # Step 4: Snap Pour Point to Maximum Flow Accumulation
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

                    # Step 5: Delineate Watershed
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
                        shapes_gen = rasterio.features.shapes(
                            ws_data,
                            mask=(ws_data > 0),
                            transform=src_ws.transform,
                        )
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

                    # Clip streams to watershed
                    stream_clipped_path = os.path.join(
                        work_dir, "streams_clipped.shp"
                    )
                    if os.path.exists(streams_vector):
                        streams_raw_gdf = gpd.read_file(
                            streams_vector
                        ).set_crs(target_crs)
                        stream_clipped_gdf = gpd.clip(
                            streams_raw_gdf, watershed_gdf
                        )
                        st.session_state["stream_gdf"] = stream_clipped_gdf
                        stream_clipped_gdf.to_file(stream_clipped_path)

                    # Step 6: Longest Flow Path
                    flowpath_shp = os.path.join(
                        work_dir, "longest_flowpath.shp"
                    )
                    wbt.longest_flowpath(
                        dem=dem_to_use,
                        basins=watershed_raster,
                        output=flowpath_shp,
                    )

                    longest_flow_m = 0.0
                    if os.path.exists(flowpath_shp):
                        fp_gdf = gpd.read_file(flowpath_shp).set_crs(
                            target_crs
                        )
                        st.session_state["flowpath_gdf"] = fp_gdf
                        if not fp_gdf.empty:
                            longest_flow_m = fp_gdf.geometry.length.sum()

                    # Step 7: Downslope Water Path
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

                    # Area Calculations
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

                    area_native_total = np.sum(ws_data > 0) * cell_area_native
                    if is_feet:
                        area_sqmi = area_native_total / 27878400.0
                        area_acres = area_native_total / 43560.0
                    else:
                        area_sqmi = area_native_total / 2589988.11
                        area_acres = area_native_total / 4046.86

                    st.session_state["watershed_results"] = {
                        "area_sqmi": area_sqmi,
                        "area_acres": area_acres,
                        "longest_flow_mi": longest_flow_m / 1609.34,
                        "longest_flow_ft": longest_flow_m * 3.28084,
                    }

                    # Bundle Zip Package into RAM Buffer
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


# def fetch_nlcd_dataset(aoi_gdf, local_tif_path="NLCD_2025_Clipped_Root.tif"):
#     if not os.path.exists(local_tif_path):
#         raise FileNotFoundError(
#             f"Root file '{local_tif_path}' not found. Ensure it is in the project repository."
#         )

#     try:
#         with rasterio.open(local_tif_path) as src:
#             aoi_local = aoi_gdf.to_crs(src.crs)
#             aoi_geom = aoi_local.geometry.unary_union
#             tif_extent = box(*src.bounds)

#             if tif_extent.covers(aoi_geom) or tif_extent.intersects(aoi_geom):
#                 nlcd_gdf = extract_local_nlcd_windowed(src, aoi_local)
#                 return nlcd_gdf, "2025 Local NLCD Dataset"
#             else:
#                 raise ValueError("Outside bounds.")
#     except Exception:
#         raise ValueError(
#             "Area is outside coverage. Download coverage from the MRLC website."
#         )

def fetch_nlcd_dataset(aoi_gdf):
    """Downloads NLCD 2021 data dynamically via MRLC WCS based on AOI bounds."""
    # Target CRS for MRLC NLCD is EPSG:5070
    aoi_5070 = aoi_gdf.to_crs("EPSG:5070")
    bounds = aoi_5070.total_bounds
    
    # Buffer bounds by 30 meters to ensure full coverage
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
    
    # Create a unique temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".tif")
    os.close(temp_fd)
    
    try:
        response = requests.get(wcs_url, stream=True, timeout=120)
        response.raise_for_status()
        
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                
        with rasterio.open(temp_path) as src:
            nlcd_gdf = extract_local_nlcd_windowed(src, aoi_5070)
            return nlcd_gdf, "MRLC WCS NLCD 2021 Dataset"
            
    except Exception as e:
        raise ValueError(f"Failed to download or process NLCD data from MRLC: {str(e)}")
    finally:
        # Cleanup temporary file to prevent memory leaks
        if os.path.exists(temp_path):
            os.remove(temp_path)



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


def calculate_weighted_cn(aoi_gdf, nlcd_gdf, ssurgo_gdf, lookup_csv_path):
    target_crs = (
        aoi_gdf.crs if (aoi_gdf.crs and aoi_gdf.crs.is_projected) else "EPSG:5070"
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

    final_intersect = gpd.overlay(
        ssurgo_grouped, nlcd_grouped, how="intersection"
    )

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
    
    del ssurgo_clipped, nlcd_clipped, ssurgo_grouped, nlcd_grouped, final_intersect
    gc.collect()
    
    return weighted_cn, merged


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
        ["Use Watershed from Watershed Delineation Tab", "Upload Zipped Shapefile"],
    )

    # if input_source == "Use Watershed from Watershed Delineation Tab":
    #     if (
    #         "aoi_gdf" in st.session_state
    #         and st.session_state["aoi_gdf"] is not None
    #     ):
    #         st.session_state["cn_aoi_gdf"] = st.session_state["aoi_gdf"]
    #     else:
    #         st.warning("No watershed delineated in Tab 1 yet.")
    #         st.session_state.pop("cn_aoi_gdf", None)
    # elif input_source == "Upload Zipped Shapefile":
    #     zip_upload = st.file_uploader(
    #         "Upload Drainage Area (ZIP containing .shp)", type=["zip"]
    #     )
    #     if zip_upload:
    #         with tempfile.TemporaryDirectory() as temp_dir:
    #             shp_path = extract_uploaded_archive_in_temp(
    #                 zip_upload, temp_dir
    #             )
    #             if shp_path:
    #                 st.session_state["cn_aoi_gdf"] = gpd.read_file(shp_path)
    #             else:
    #                 st.error("No valid .shp file found in the ZIP.")
    
    if input_source == "Use Watershed from Watershed Delineation Tab":
        if (
            "aoi_gdf" in st.session_state
            and st.session_state["aoi_gdf"] is not None
        ):
            if st.session_state.get("cn_aoi_gdf") is not st.session_state["aoi_gdf"]:
                st.session_state["cn_aoi_gdf"] = st.session_state["aoi_gdf"]
                b = st.session_state["cn_aoi_gdf"].to_crs("EPSG:4326").total_bounds
                st.session_state["cn_zoom_bounds"] = [[b[1], b[0]], [b[3], b[2]]]
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
                    st.session_state["cn_zoom_bounds"] = [[b[1], b[0]], [b[3], b[2]]]
                else:
                    st.error("No valid .shp file found in the ZIP.")
                    

    has_aoi = "cn_aoi_gdf" in st.session_state
    has_nlcd = "cn_nlcd_gdf" in st.session_state
    has_ssurgo = "cn_ssurgo_gdf" in st.session_state
    has_cn = "cn_intersected_gdf" in st.session_state
    
    ######### new Added
    

    st.markdown("---")
    st.subheader("User Curve Number Lookup")
    
    lookup_col1, lookup_col2 = st.columns([2, 1])
    with lookup_col1:
        lookup_upload = st.file_uploader(
            "Upload Custom NLCD-HSG CN Lookup Table (.csv)", 
            type=["csv"],
            help="Optional. If not uploaded, the default root folder table will be used."
        )
        
    with lookup_col2:
        default_lookup_path = "NLCD_SHG_CN_lookup.csv"
        # Push the download button down slightly to align with the file uploader box
        st.write("") 
        st.write("")
        if os.path.exists(default_lookup_path):
            with open(default_lookup_path, "rb") as f:
                st.download_button(
                    label="📄 Download Sample Lookup Table",
                    data=f,
                    file_name="Sample_NLCD_SHG_CN_lookup.csv",
                    mime="text/csv",
                    use_container_width=True
                )
        else:
            st.info("Sample table not found in root directory.")

    
    ########################
    
    st.markdown("---")
    col_btn1, col_btn2, col_btn3 = st.columns(3)

    # with col_btn1:
    #     if st.button(
    #         "📥 1. Download NLCD", disabled=not has_aoi, use_container_width=True
    #     ):
    #         with st.spinner(
    #             "Checking area extent and processing NLCD Land Cover dataset..."
    #         ):
    #             try:
    #                 nlcd_gdf, source_ver = fetch_nlcd_dataset(
    #                     st.session_state["cn_aoi_gdf"],
    #                     local_tif_path="NLCD_2025_Clipped_Root.tif",
    #                 )
    #                 st.session_state["cn_nlcd_gdf"] = nlcd_gdf
    #                 st.session_state["nlcd_source_version"] = source_ver
    #                 st.rerun()
    #             except Exception as e:
    #                 st.error(str(e))

    # with col_btn2:
    #     if st.button(
    #         "📥 2. Download SSURGO",
    #         disabled=not has_aoi,
    #         use_container_width=True,
    #     ):
    #         with st.spinner("Downloading SSURGO Soils via USDA WFS in RAM..."):
    #             try:
    #                 st.session_state["cn_ssurgo_gdf"] = download_ssurgo_extended(
    #                     st.session_state["cn_aoi_gdf"]
    #                 )
    #                 st.rerun()
    #             except Exception as e:
    #                 st.error(f"SSURGO Download Failed: {e}")

    # with col_btn3:
    #     if st.button(
    #         "📊 3. Calculate CN",
    #         disabled=not (has_aoi and has_nlcd and has_ssurgo),
    #         type="primary",
    #         use_container_width=True,
    #     ):
    #         lookup_path = "NLCD_SHG_CN_lookup.csv"
    #         if not os.path.exists(lookup_path):
    #             st.error(f"Lookup table '{lookup_path}' not found in root folder.")
    #         else:
    #             with st.spinner(
    #                 "Intersecting layers and calculating weighted CN..."
    #             ):
    #                 try:
    #                     weighted_cn, intersected_gdf = calculate_weighted_cn(
    #                         st.session_state["cn_aoi_gdf"],
    #                         st.session_state["cn_nlcd_gdf"],
    #                         st.session_state["cn_ssurgo_gdf"],
    #                         lookup_path,
    #                     )
    #                     st.session_state["final_cn"] = weighted_cn
    #                     st.session_state["cn_intersected_gdf"] = intersected_gdf

    #                     st.session_state["cn_zip_bytes"] = (
    #                         bundle_cn_project_zip_in_memory(
    #                             st.session_state["cn_aoi_gdf"],
    #                             st.session_state["cn_ssurgo_gdf"],
    #                             st.session_state["cn_nlcd_gdf"],
    #                             intersected_gdf,
    #                         )
    #                     )
    #                     st.rerun()
    #                 except Exception as e:
    #                     st.error(f"Calculation Error: {e}")
    
    with col_btn1:
        if st.button(
            "📥 1. Download NLCD", disabled=not has_aoi, use_container_width=True
        ):
            with st.spinner(
                "Downloading NLCD Land Cover dataset from MRLC WCS..."
            ):
                try:
                    nlcd_gdf, source_ver = fetch_nlcd_dataset(st.session_state["cn_aoi_gdf"])
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
                    st.session_state["cn_ssurgo_gdf"] = download_ssurgo_extended(
                        st.session_state["cn_aoi_gdf"]
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
            with st.spinner("Intersecting layers and calculating weighted CN..."):
                temp_lookup_path = None
                try:
                    # Switch logic between uploaded file or root file
                    if lookup_upload is not None:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                            tmp.write(lookup_upload.getvalue())
                            temp_lookup_path = tmp.name
                        active_lookup_path = temp_lookup_path
                    else:
                        active_lookup_path = "NLCD_SHG_CN_lookup.csv"
                    
                    if not os.path.exists(active_lookup_path):
                        st.error(f"Lookup table '{active_lookup_path}' not found. Please upload one or ensure it exists in the app root.")
                    else:
                        weighted_cn, intersected_gdf = calculate_weighted_cn(
                            st.session_state["cn_aoi_gdf"],
                            st.session_state["cn_nlcd_gdf"],
                            st.session_state["cn_ssurgo_gdf"],
                            active_lookup_path,
                        )
                        st.session_state["final_cn"] = weighted_cn
                        st.session_state["cn_intersected_gdf"] = intersected_gdf

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
                    # Clean up the temp lookup file if a user upload was generated
                    if temp_lookup_path and os.path.exists(temp_lookup_path):
                        os.remove(temp_lookup_path)
                        
    ############################

    if "nlcd_source_version" in st.session_state:
        st.info(
            f"ℹ️ **NLCD Data Source Used:** {st.session_state['nlcd_source_version']}"
        )

    if "final_cn" in st.session_state:
        st.markdown("---")
        col_res1, col_res2 = st.columns([1, 2])
        with col_res1:
            st.metric(
                "Composite Area-Weighted Curve Number (CN)",
                f"{st.session_state['final_cn']:.2f}",
            )
        with col_res2:
            if "cn_zip_bytes" in st.session_state:
                st.download_button(
                    label="📦 Download Complete CN GIS Package (Watershed, SSURGO, NLCD & CN Shapefiles)",
                    data=st.session_state["cn_zip_bytes"],
                    file_name="cn_hydrology_project_all_shapefiles.zip",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                )

    # st.markdown("---")
    # st.subheader("Interactive Map Viewer")
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

        # NLCD Land Cover Layer
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

        # SSURGO Soil Groups Layer
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

        # Watershed Boundary Layer
        # if has_aoi:
        #     aoi_4326 = st.session_state["cn_aoi_gdf"].to_crs("EPSG:4326")
        #     folium.GeoJson(
        #         aoi_4326,
        #         name="Watershed Boundary",
        #         style_function=lambda x: {
        #             "color": "red",
        #             "fillOpacity": 0,
        #             "weight": 3.5,
        #         },
        #     ).add_to(m2)
        #     m2.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
        
        # Watershed Boundary Layer
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

        # Calculated CN Polygons Layer
        if has_cn:
            cn_4326 = (
                st.session_state["cn_intersected_gdf"].to_crs("EPSG:4326").copy()
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
                    fields=["grid_code", "cn", "land_use_clean", "hyg_clean"],
                    aliases=[
                        "Grid Code:",
                        "Curve Number (CN):",
                        "NLCD Code:",
                        "Soil HSG:",
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