import os
import fitparse
import folium
import logging

directory = "activities"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

m = folium.Map(location=[48.1351, 11.5820], zoom_start=12)

def plot(fitpath):
    lat_lon_points = []
    try:
        fitfile = fitparse.FitFile(fitpath)
        for record in fitfile.get_messages("record"):
            lat = record.get_value("position_lat")
            lon = record.get_value("position_long")
            if lat is not None and lon is not None:
                lat2 = lat * (180 / 2**31)
                lon2 = lon * (180 / 2**31)
                lat_lon_points.append((lat2, lon2))
        
        # Check if there are points to plot
        if lat_lon_points:
            poly = folium.PolyLine(lat_lon_points, color="blue", weight=2.5, opacity=1).add_to(m)
            filename = os.path.basename(fitpath)
            poly.add_child(folium.Popup(filename))
            poly.add_to(m)
    except Exception as e:
        logger.error(f"Could not parse {fitpath}: {e}")
 
def iterate_fit_files(directory):
    for filename in os.listdir(directory):
        if filename.endswith(".fit"):
            filepath = os.path.join(directory, filename)
            plot(filepath)

def save_map():
    m.save("analysis/activities_map.html")
    logger.info("Map saved to processed/activities_map.html")

iterate_fit_files(directory)
save_map()