import os
import fitparse
import pandas as pd
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

directory = "activities"

def read_fitfile(fitpath):
    df = pd.DataFrame()
    fitfile = fitparse.FitFile(fitpath)
    for session in fitfile.get_messages("session"):
        start_time_dt = session.get_value("start_time")
        duration_s = session.get_value("total_timer_time")
        activity_type = session.get_value("sport")
        distance_m = session.get_value("total_distance")
        logger.debug(f"Reading file: {fitpath} with following data: Start time: {start_time_dt}, Duration (s): {duration_s}, Distance (m): {distance_m}, Activity type: {activity_type}")
        new_session = {
            "start_time": start_time_dt,
            "duration_s": duration_s,
            "distance_m": distance_m,
            "activity_type": activity_type,
        }
        df_new = pd.DataFrame([new_session], columns=["start_time", "duration_s", "distance_m", "activity_type"])
        df = pd.concat([df, df_new], ignore_index=True)
    return df


def iterate_fit_files(directory):
    df = pd.DataFrame()
    for filename in os.listdir(directory):
        if filename.endswith(".fit"):
            filepath = os.path.join(directory, filename)
            df_new = read_fitfile(filepath)
            df = pd.concat([df, df_new], ignore_index=True)
    return df


df = iterate_fit_files(directory)
df.to_csv("analysis/activities.csv", index=False)