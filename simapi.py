"""
Definitions for elements of the Simulator API

The server side of the API is implemented in mnet/driver.py
The client side is implemented in mnet/client.py
"""

from pydantic import BaseModel
from typing import List, Optional

class Link(BaseModel):
    node1_name: str
    node2_name: str
    up: bool

# [UPDATED] Add Az/El and Lat/Lon to the Uplink definition
class UpLink(BaseModel):
    sat_node: str
    distance: int
    az_deg: float      # NEW: Real Azimuth
    el_deg: float      # NEW: Real Elevation
    
class UpLinks(BaseModel):
    ground_node: str
    # [UPDATED] Add GS Coordinates so we don't have to hardcode them
    gs_lat: float      
    gs_lon: float      
    uplinks: List[UpLink]

