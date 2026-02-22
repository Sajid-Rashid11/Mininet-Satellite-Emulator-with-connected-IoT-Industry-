import math
import json
from dataclasses import dataclass
from typing import List

@dataclass
class StormCell:
    name: str
    lat: float
    lon: float
    radius_km: float
    height_km: float
    rain_rate_mmh: float

class RainModel:
    def __init__(self):
        # ITU-R P.838-3 Coefficients for ~12 GHz (Ku-Band)
        self.k = 0.0188
        self.alpha = 1.217
        self.earth_radius_km = 6371.0

    def load_config(self, filepath: str) -> List[StormCell]:
        """Loads storm cells from a JSON file."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                
            cells = []
            for c in data.get("cells", []):
                cells.append(StormCell(
                    name=c["name"],
                    lat=c["lat"],
                    lon=c["lon"],
                    radius_km=c["radius_km"],
                    height_km=c["height_km"],
                    rain_rate_mmh=c["rain_rate_mmh"]
                ))
            print(f"[RAIN] Loaded {len(cells)} storm cells from {filepath}")
            return cells
        except FileNotFoundError:
            print(f"[RAIN] Warning: {filepath} not found. No rain applied.")
            return []

    def get_specific_attenuation(self, rain_rate):
        """Returns loss in dB per km"""
        if rain_rate <= 0: return 0.0
        return self.k * math.pow(rain_rate, self.alpha)

    def calculate_path_loss(self, gs_lat, gs_lon, sat_az, sat_el, storm):
        """
        Calculates attenuation using full 3D Ray-Cylinder intersection.
        Valid for GS inside OR outside the storm.
        """
        if sat_el < 0: return 0.0  # Satellite below horizon
        if sat_el > 85: return 0.0 # Zenith (overhead) usually avoids remote storms

        # --- STEP 1: 2D HORIZONTAL INTERSECTION ---
        deg_to_km = 111.32
        d_lat_km = (storm.lat - gs_lat) * deg_to_km
        d_lon_km = (storm.lon - gs_lon) * deg_to_km * math.cos(math.radians(gs_lat))
        
        Cx, Cy = d_lon_km, d_lat_km
        
        az_rad = math.radians(sat_az)
        Dx = math.sin(az_rad)
        Dy = math.cos(az_rad)

        b = -2.0 * (Dx * Cx + Dy * Cy)
        c = (Cx**2 + Cy**2) - (storm.radius_km**2)
        
        discriminant = b**2 - 4*c
        
        if discriminant < 0: return 0.0 
            
        sqrt_disc = math.sqrt(discriminant)
        t1 = (-b - sqrt_disc) / 2.0 
        t2 = (-b + sqrt_disc) / 2.0 
        
        if t2 < 0: return 0.0 
            
        t_enter = max(0.0, t1)
        t_exit = t2
        
        if t_enter >= t_exit: return 0.0
            
        # --- STEP 2: 3D VERTICAL CHECK ---
        el_rad = math.radians(max(sat_el, 0.5)) 
        max_dist_due_to_height = storm.height_km / math.tan(el_rad)
        
        effective_enter = t_enter
        effective_exit = min(t_exit, max_dist_due_to_height)
        
        if effective_enter >= effective_exit: return 0.0 
            
        ground_dist_in_rain = effective_exit - effective_enter
        slant_path_km = ground_dist_in_rain / math.cos(el_rad)
        
        # --- STEP 3: ATTENUATION ---
        specific_loss = self.get_specific_attenuation(storm.rain_rate_mmh)
        return specific_loss * slant_path_km