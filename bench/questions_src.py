"""Source of truth for the mapsql-mcp benchmark.

Every answerable question has

* ``gold_sql``  - hand-written SQL against the demo database that produces the gold answer, and
* ``solution``  - one reference call of the mapsql-mcp tools (+ a path to extract the answer from the result).

``make_gold.py`` runs the SQL and writes ``questions.json``; ``run_bench.py --oracle`` replays the
``solution`` calls through the real tools and checks they reproduce the gold answers, so the benchmark
simultaneously validates the data, the gold values and that the toolset can express every question.

Answer types:  number (``tol`` absolute, ``rel`` relative) | set (order-insensitive names) | text | clarify
(the model must ask for the missing information instead of answering).
"""

K = "ST_SetSRID(ST_MakePoint(32.8541, 39.9208), 4326)::geography"  # Kizilay square, Ankara
KIZILAY = "Kizilay Square, Ankara (lat 39.9208, lon 32.8541)"
KLAT, KLON = 39.9208, 32.8541


def f(column, value=None, op="="):
    d = {"column": column, "op": op}
    if value is not None:
        d["value"] = value
    return d


def poi_within(kind, d):
    return f"SELECT count(*) FROM ankara.pois WHERE kind = '{kind}' AND ST_DWithin(geom::geography, {K}, {d})"


Q = []


def add(id, category, question, type, gold_sql=None, solution=None, tol=0.0, rel=0.0, answer_hint=None):
    Q.append(
        dict(id=id, category=category, question=question, type=type, gold_sql=gold_sql, solution=solution, tol=tol, rel=rel,
             answer_hint=answer_hint)
    )


def sol(tool, args, path):
    return {"tool": tool, "args": args, "path": path}


# ---------------------------------------------------------------- attribute queries
add("A01", "attribute", "How many countries are there in the world.countries layer?", "number",
    "SELECT count(*) FROM world.countries", sol("aggregate", {"layer": "world.countries"}, "rows.0.count"))
add("A02", "attribute", "How many countries are in Africa?", "number",
    "SELECT count(*) FROM world.countries WHERE continent = 'Africa'",
    sol("aggregate", {"layer": "world.countries", "filters": [f("continent", "Africa")]}, "rows.0.count"))
add("A03", "attribute", "What is the estimated population (pop_est) of Turkey?", "number",
    "SELECT pop_est FROM world.countries WHERE name = 'Turkey'",
    sol("find_features", {"layer": "world.countries", "filters": [f("name", "Turkey")], "columns": ["pop_est"], "geometry": "none"},
        "features.0.properties.pop_est"))
add("A04", "attribute", "Which country in South America has the largest population?", "text",
    "SELECT name FROM world.countries WHERE continent = 'South America' ORDER BY pop_est DESC LIMIT 1",
    sol("find_features", {"layer": "world.countries", "filters": [f("continent", "South America")], "columns": ["name"],
                          "order_by": "pop_est", "descending": True, "limit": 1, "geometry": "none"}, "features.0.properties.name"))
add("A05", "attribute", "How many populated places are national capitals (is_capital = true)?", "number",
    "SELECT count(*) FROM world.cities WHERE is_capital",
    sol("aggregate", {"layer": "world.cities", "filters": [f("is_capital", True)]}, "rows.0.count"))
add("A06", "attribute", "How many populated places have a pop_max above 5 million?", "number",
    "SELECT count(*) FROM world.cities WHERE pop_max > 5000000",
    sol("aggregate", {"layer": "world.cities", "filters": [f("pop_max", 5000000, ">")]}, "rows.0.count"))
add("A07", "attribute", "Which are the three most populous cities in Japan (by pop_max)? Give their names.", "set",
    "SELECT name FROM world.cities WHERE country = 'Japan' ORDER BY pop_max DESC LIMIT 3",
    sol("find_features", {"layer": "world.cities", "filters": [f("country", "Japan")], "columns": ["name"], "order_by": "pop_max",
                          "descending": True, "limit": 3, "geometry": "none"}, "features[*].properties.name"))
add("A08", "attribute", "How many pharmacies (kind = 'pharmacy') are there in the Ankara POI data?", "number",
    "SELECT count(*) FROM ankara.pois WHERE kind = 'pharmacy'",
    sol("aggregate", {"layer": "ankara.pois", "filters": [f("kind", "pharmacy")]}, "rows.0.count"))
add("A09", "attribute", "How many restaurants in the Ankara POI data have cuisine 'kebab'?", "number",
    "SELECT count(*) FROM ankara.pois WHERE kind = 'restaurant' AND cuisine = 'kebab'",
    sol("aggregate", {"layer": "ankara.pois", "filters": [f("kind", "restaurant"), f("cuisine", "kebab")]}, "rows.0.count"))
add("A10", "attribute", "How many distinct kinds of POI exist in the 'amenity' category of the Ankara POI data?", "number",
    "SELECT count(DISTINCT kind) FROM ankara.pois WHERE category = 'amenity'",
    sol("aggregate", {"layer": "ankara.pois", "filters": [f("category", "amenity")],
                      "metrics": [{"fn": "count_distinct", "column": "kind"}]}, "rows.0.distinct_kind"))
add("A11", "attribute", "How many road segments in the Ankara roads layer are of highway type 'primary'?", "number",
    "SELECT count(*) FROM ankara.roads WHERE highway = 'primary'",
    sol("aggregate", {"layer": "ankara.roads", "filters": [f("highway", "primary")]}, "rows.0.count"))
add("A12", "attribute", "How many parks in the Ankara parks layer have a name (name is not null) and leisure = 'park'?", "number",
    "SELECT count(*) FROM ankara.parks WHERE name IS NOT NULL AND leisure = 'park'",
    sol("aggregate", {"layer": "ankara.parks", "filters": [f("name", op="not_null"), f("leisure", "park")]}, "rows.0.count"))
add("A13", "attribute", "How many admin-1 regions does the United States of America have in the world.provinces layer?", "number",
    "SELECT count(*) FROM world.provinces WHERE country = 'United States of America'",
    sol("aggregate", {"layer": "world.provinces", "filters": [f("country", "United States of America")]}, "rows.0.count"))

# ---------------------------------------------------------------- distance / nearest
add("D01", "distance", f"How many pharmacies are within 500 metres of {KIZILAY}?", "number", poi_within("pharmacy", 500),
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 500, "lon": KLON, "lat": KLAT, "filters": [f("kind", "pharmacy")],
                            "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("D02", "distance", f"How many cafes are within 300 metres of {KIZILAY}?", "number", poi_within("cafe", 300),
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 300, "lon": KLON, "lat": KLAT, "filters": [f("kind", "cafe")],
                            "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("D03", "distance", f"How many restaurants are within 1 kilometre of {KIZILAY}?", "number", poi_within("restaurant", 1000),
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 1000, "lon": KLON, "lat": KLAT, "filters": [f("kind", "restaurant")],
                            "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("D04", "distance", f"How many banks are within 200 metres of {KIZILAY}?", "number", poi_within("bank", 200),
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 200, "lon": KLON, "lat": KLAT, "filters": [f("kind", "bank")],
                            "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("D05", "distance", f"What is the distance in metres from {KIZILAY} to the nearest hospital (kind = 'hospital')? Round to the nearest 10 m.", "number",
    f"SELECT round(min(ST_Distance(geom::geography, {K}))::numeric, -1) FROM ankara.pois WHERE kind = 'hospital'",
    sol("nearest", {"layer": "ankara.pois", "k": 1, "lon": KLON, "lat": KLAT, "filters": [f("kind", "hospital")], "geometry": "none"},
        "features.0.properties.distance_m"), tol=15)
add("D06", "distance", f"Which subway station (kind = 'subway_station') is nearest to {KIZILAY}?", "text",
    f"SELECT name FROM ankara.pois WHERE kind = 'subway_station' ORDER BY ST_Distance(geom::geography, {K}) LIMIT 1",
    sol("nearest", {"layer": "ankara.pois", "k": 1, "lon": KLON, "lat": KLAT, "filters": [f("kind", "subway_station")],
                    "columns": ["name"], "geometry": "none"}, "features.0.properties.name"))
add("D07", "distance", f"Which are the three pharmacies nearest to {KIZILAY}? Give their names.", "set",
    f"SELECT name FROM (SELECT name FROM ankara.pois WHERE kind = 'pharmacy' ORDER BY ST_Distance(geom::geography, {K}) LIMIT 3) t",
    sol("nearest", {"layer": "ankara.pois", "k": 3, "lon": KLON, "lat": KLAT, "filters": [f("kind", "pharmacy")], "columns": ["name"],
                    "geometry": "none"}, "features[*].properties.name"))
add("D08", "distance", "How many schools (kind = 'school') are within 1500 metres of the point lat 39.925, lon 32.83?", "number",
    "SELECT count(*) FROM ankara.pois WHERE kind = 'school' AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(32.83, 39.925), 4326)::geography, 1500)",
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 1500, "lon": 32.83, "lat": 39.925, "filters": [f("kind", "school")],
                            "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("D09", "distance", "What is the geodesic distance in kilometres between the populated places Ankara and Istanbul (country Turkey)? Round to the nearest km.", "number",
    "SELECT round((ST_Distance(a.geom::geography, b.geom::geography) / 1000)::numeric) FROM world.cities a, world.cities b "
    "WHERE a.name = 'Ankara' AND a.country = 'Turkey' AND b.name = 'Istanbul' AND b.country = 'Turkey'",
    sol("nearest", {"layer": "world.cities", "k": 1, "reference_layer": "world.cities",
                    "reference_filters": [f("name", "Ankara"), f("country", "Turkey")],
                    "filters": [f("name", "Istanbul"), f("country", "Turkey")], "columns": ["name"], "geometry": "none"},
        "features.0.properties.distance_m"), tol=2.0, answer_hint="km; tool distances are in metres")
add("D10", "distance", "How many populated places other than Ankara itself, with pop_max above 1,000,000, lie within 500 km of Ankara (Turkey)?", "number",
    "SELECT count(*) FROM world.cities c, (SELECT geom FROM world.cities WHERE name = 'Ankara' AND country = 'Turkey') a "
    "WHERE c.pop_max > 1000000 AND NOT (c.name = 'Ankara' AND c.country = 'Turkey') AND ST_DWithin(c.geom::geography, a.geom::geography, 500000)",
    sol("within_distance", {"layer": "world.cities", "distance_m": 500000, "reference_layer": "world.cities",
                            "reference_filters": [f("name", "Ankara"), f("country", "Turkey")],
                            "filters": [f("pop_max", 1000000, ">"), f("name", "Ankara", "!=")], "columns": ["name"], "limit": 5000,
                            "geometry": "none"}, "count"))

# ---------------------------------------------------------------- spatial relations
add("S01", "spatial", "How many POIs from the Ankara POI layer lie inside the park named 'Gençlik Parkı'?", "number",
    "SELECT count(*) FROM ankara.pois p JOIN ankara.parks k ON ST_Within(p.geom, k.geom) WHERE k.name = 'Gençlik Parkı'",
    sol("spatial_filter", {"layer": "ankara.pois", "relation": "within", "other_layer": "ankara.parks",
                           "other_filters": [f("name", "Gençlik Parkı")], "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("S02", "spatial", "Which country polygon in world.countries contains the point lat 48.85, lon 2.35?", "text",
    "SELECT name FROM world.countries WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(2.35, 48.85), 4326))",
    sol("spatial_filter", {"layer": "world.countries", "relation": "contains",
                           "geojson_geometry": {"type": "Point", "coordinates": [2.35, 48.85]}, "columns": ["name"], "geometry": "none"},
        "features.0.properties.name"))
add("S03", "spatial", f"In which neighbourhood (ankara.neighbourhoods) is {KIZILAY}?", "text",
    f"SELECT name FROM ankara.neighbourhoods WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint({KLON}, {KLAT}), 4326))",
    sol("spatial_filter", {"layer": "ankara.neighbourhoods", "relation": "contains",
                           "geojson_geometry": {"type": "Point", "coordinates": [KLON, KLAT]}, "columns": ["name"], "geometry": "none"},
        "features.0.properties.name"))
add("S04", "spatial", "How many countries does the river named 'Danube' intersect in world.rivers / world.countries?", "number",
    "SELECT count(DISTINCT c.name) FROM world.countries c JOIN world.rivers r ON ST_Intersects(c.geom, r.geom) WHERE r.name = 'Danube'",
    sol("spatial_filter", {"layer": "world.countries", "relation": "intersects", "other_layer": "world.rivers",
                           "other_filters": [f("name", "Danube")], "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("S05", "spatial", "How many road segments of the Ankara roads layer intersect the neighbourhood 'Kocatepe Mahallesi'?", "number",
    "SELECT count(*) FROM ankara.roads r JOIN ankara.neighbourhoods n ON ST_Intersects(r.geom, n.geom) WHERE n.name = 'Kocatepe Mahallesi'",
    sol("spatial_filter", {"layer": "ankara.roads", "relation": "intersects", "other_layer": "ankara.neighbourhoods",
                           "other_filters": [f("name", "Kocatepe Mahallesi")], "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("S06", "spatial", "How many lakes of the world.lakes layer intersect the country Canada?", "number",
    "SELECT count(*) FROM world.lakes l JOIN world.countries c ON ST_Intersects(l.geom, c.geom) WHERE c.name = 'Canada'",
    sol("spatial_filter", {"layer": "world.lakes", "relation": "intersects", "other_layer": "world.countries",
                           "other_filters": [f("name", "Canada")], "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))
add("S07", "spatial", "How many populated places of world.cities lie inside the bounding box lon 5..15, lat 45..55?", "number",
    "SELECT count(*) FROM world.cities WHERE geom && ST_MakeEnvelope(5, 45, 15, 55, 4326)",
    sol("spatial_filter", {"layer": "world.cities", "relation": "intersects", "bbox": [5, 45, 15, 55], "columns": ["name"], "limit": 5000,
                           "geometry": "none"}, "count"))
add("S08", "spatial", "How many neighbourhoods of ankara.neighbourhoods touch the neighbourhood 'Kocatepe Mahallesi' (share a boundary, excluding itself)?", "number",
    "SELECT count(*) FROM ankara.neighbourhoods a, ankara.neighbourhoods b WHERE b.name = 'Kocatepe Mahallesi' AND a.osm_id <> b.osm_id AND ST_Touches(a.geom, b.geom)",
    sol("spatial_filter", {"layer": "ankara.neighbourhoods", "relation": "touches", "other_layer": "ankara.neighbourhoods",
                           "other_filters": [f("name", "Kocatepe Mahallesi")], "columns": ["name"], "limit": 5000, "geometry": "none"}, "count"))

# ---------------------------------------------------------------- aggregation
add("G01", "aggregate", "What is the total population (sum of pop_est) of all countries in Europe?", "number",
    "SELECT sum(pop_est) FROM world.countries WHERE continent = 'Europe'",
    sol("aggregate", {"layer": "world.countries", "filters": [f("continent", "Europe")], "metrics": [{"fn": "sum", "column": "pop_est"}]},
        "rows.0.sum_pop_est"), rel=0.0001)
add("G02", "aggregate", "How many countries are in Asia and Oceania combined?", "number",
    "SELECT count(*) FROM world.countries WHERE continent IN ('Asia', 'Oceania')",
    sol("aggregate", {"layer": "world.countries", "filters": [f("continent", ["Asia", "Oceania"], "in")]}, "rows.0.count"))
add("G03", "aggregate", "What is the average pop_max of populated places that are national capitals? Round to the nearest whole number.", "number",
    "SELECT round(avg(pop_max)) FROM world.cities WHERE is_capital",
    sol("aggregate", {"layer": "world.cities", "filters": [f("is_capital", True)], "metrics": [{"fn": "avg", "column": "pop_max"}]},
        "rows.0.avg_pop_max"), tol=1.0)
add("G04", "aggregate", "Which continent has the highest total GDP (sum of gdp_musd) in world.countries?", "text",
    "SELECT continent FROM world.countries GROUP BY continent ORDER BY sum(gdp_musd) DESC LIMIT 1",
    sol("aggregate", {"layer": "world.countries", "group_by": ["continent"], "metrics": [{"fn": "sum", "column": "gdp_musd"}], "limit": 1},
        "rows.0.continent"))
add("G05", "aggregate", "What is the total geodesic area in km2 of all parks in the Ankara parks layer? Round to 1 decimal.", "number",
    "SELECT round((sum(ST_Area(geom::geography)) / 1e6)::numeric, 1) FROM ankara.parks",
    sol("aggregate", {"layer": "ankara.parks", "metrics": [{"fn": "area_km2"}]}, "rows.0.area_km2"), tol=0.06)
add("G06", "aggregate", "What is the total geodesic length in km of roads with highway = 'residential' in the Ankara roads layer? Round to 1 decimal.", "number",
    "SELECT round((sum(ST_Length(geom::geography)) / 1000)::numeric, 1) FROM ankara.roads WHERE highway = 'residential'",
    sol("aggregate", {"layer": "ankara.roads", "filters": [f("highway", "residential")], "metrics": [{"fn": "length_km"}]},
        "rows.0.length_km"), tol=0.6)
add("G07", "aggregate", "Which highway type has the greatest total length in the Ankara roads layer?", "text",
    "SELECT highway FROM ankara.roads GROUP BY highway ORDER BY sum(ST_Length(geom::geography)) DESC LIMIT 1",
    sol("aggregate", {"layer": "ankara.roads", "group_by": ["highway"], "metrics": [{"fn": "length_km"}], "limit": 1}, "rows.0.highway"))
add("G08", "aggregate", "What is the geodesic area of Turkey in km2 according to the world.countries polygon? Round to the nearest 1000 km2.", "number",
    "SELECT round((ST_Area(geom::geography) / 1e6)::numeric, -3) FROM world.countries WHERE name = 'Turkey'",
    sol("aggregate", {"layer": "world.countries", "filters": [f("name", "Turkey")], "metrics": [{"fn": "area_km2"}]}, "rows.0.area_km2"),
    tol=2000)
add("G09", "aggregate", "Which African country has the largest area (geodesic) in world.countries?", "text",
    "SELECT name FROM world.countries WHERE continent = 'Africa' ORDER BY ST_Area(geom::geography) DESC LIMIT 1",
    sol("aggregate", {"layer": "world.countries", "filters": [f("continent", "Africa")], "group_by": ["name"],
                      "metrics": [{"fn": "area_km2"}], "limit": 1}, "rows.0.name"))
add("G10", "aggregate", "How many distinct countries appear in the country column of world.cities?", "number",
    "SELECT count(DISTINCT country) FROM world.cities",
    sol("aggregate", {"layer": "world.cities", "metrics": [{"fn": "count_distinct", "column": "country"}]}, "rows.0.distinct_country"))

# ---------------------------------------------------------------- joins
add("J01", "join", "Which neighbourhood contains the most pharmacies (kind = 'pharmacy')?", "text",
    "SELECT n.name FROM ankara.neighbourhoods n LEFT JOIN ankara.pois p ON ST_Contains(n.geom, p.geom) AND p.kind = 'pharmacy' "
    "GROUP BY n.osm_id, n.name ORDER BY count(p.*) DESC, n.name LIMIT 1",
    sol("spatial_join", {"left_layer": "ankara.neighbourhoods", "right_layer": "ankara.pois", "relation": "contains",
                         "right_filters": [f("kind", "pharmacy")], "limit": 1, "geometry": "none"}, "features.0.properties.name"))
add("J02", "join", "How many neighbourhoods contain at least one cafe (kind = 'cafe')?", "number",
    "SELECT count(*) FROM ankara.neighbourhoods n WHERE EXISTS (SELECT 1 FROM ankara.pois p WHERE p.kind = 'cafe' AND ST_Contains(n.geom, p.geom))",
    sol("spatial_join", {"left_layer": "ankara.neighbourhoods", "right_layer": "ankara.pois", "relation": "contains",
                         "right_filters": [f("kind", "cafe")], "min_count": 1, "limit": 5000, "geometry": "none"}, "count"))
add("J03", "join", "How many pharmacies lie inside the neighbourhood 'Hacettepe Mahallesi'?", "number",
    "SELECT count(*) FROM ankara.pois p JOIN ankara.neighbourhoods n ON ST_Contains(n.geom, p.geom) WHERE p.kind = 'pharmacy' AND n.name = 'Hacettepe Mahallesi'",
    sol("spatial_join", {"left_layer": "ankara.neighbourhoods", "right_layer": "ankara.pois", "relation": "contains",
                         "left_filters": [f("name", "Hacettepe Mahallesi")], "right_filters": [f("kind", "pharmacy")], "geometry": "none"},
        "features.0.properties.right_count"))
add("J04", "join", "Using a spatial join between world.countries and world.cities, which country polygon contains the most populated places?", "text",
    "SELECT c.name FROM world.countries c JOIN world.cities p ON ST_Contains(c.geom, p.geom) GROUP BY c.name ORDER BY count(*) DESC, c.name LIMIT 1",
    sol("spatial_join", {"left_layer": "world.countries", "right_layer": "world.cities", "relation": "contains", "limit": 1,
                         "geometry": "none"}, "features.0.properties.name"))
add("J05", "join", "How many parks in the Ankara parks layer contain at least one cafe (kind = 'cafe')?", "number",
    "SELECT count(*) FROM ankara.parks k WHERE EXISTS (SELECT 1 FROM ankara.pois p WHERE p.kind = 'cafe' AND ST_Contains(k.geom, p.geom))",
    sol("spatial_join", {"left_layer": "ankara.parks", "right_layer": "ankara.pois", "relation": "contains",
                         "right_filters": [f("kind", "cafe")], "min_count": 1, "limit": 5000, "geometry": "none"}, "count"))
add("J06", "join", "What is the total pop_max of the populated places inside Germany (spatial join of world.countries and world.cities)? ", "number",
    "SELECT sum(p.pop_max) FROM world.cities p JOIN world.countries c ON ST_Contains(c.geom, p.geom) WHERE c.name = 'Germany'",
    sol("spatial_join", {"left_layer": "world.countries", "right_layer": "world.cities", "relation": "contains", "mode": "sum",
                         "right_column": "pop_max", "left_filters": [f("name", "Germany")], "geometry": "none"},
        "features.0.properties.right_sum_pop_max"))

# ---------------------------------------------------------------- buffer / bbox
add("B01", "buffer", f"What is the area in km2 of a 1 km radius buffer around {KIZILAY}? Round to 2 decimals.", "number",
    f"SELECT round((ST_Area(ST_Buffer({K}, 1000)::geography) / 1e6)::numeric, 2)",
    sol("buffer", {"distance_m": 1000, "lon": KLON, "lat": KLAT}, "features.0.properties.area_km2"), tol=0.05)
add("B02", "buffer", "How many banks (kind = 'bank') are within 250 metres of the park named 'Gençlik Parkı' (distance measured to the park polygon)?", "number",
    "SELECT count(*) FROM ankara.pois p, ankara.parks k WHERE p.kind = 'bank' AND k.name = 'Gençlik Parkı' AND ST_DWithin(p.geom::geography, k.geom::geography, 250)",
    sol("within_distance", {"layer": "ankara.pois", "distance_m": 250, "reference_layer": "ankara.parks",
                            "reference_filters": [f("name", "Gençlik Parkı")], "filters": [f("kind", "bank")], "columns": ["name"],
                            "limit": 5000, "geometry": "none"}, "count"))
add("B03", "buffer", "What is the area in km2 of the union of 200 m buffers around all hospitals (kind = 'hospital') in the Ankara POI data? Round to 2 decimals.", "number",
    "SELECT round((ST_Area(ST_Union(ST_Buffer(geom::geography, 200)::geometry)::geography) / 1e6)::numeric, 2) FROM ankara.pois WHERE kind = 'hospital'",
    sol("buffer", {"distance_m": 200, "layer": "ankara.pois", "filters": [f("kind", "hospital")]}, "features.0.properties.area_km2"), rel=0.02)
add("X01", "bbox", "What is the northernmost latitude among all POIs of the Ankara POI layer? Round to 3 decimals.", "number",
    "SELECT round(max(ST_Y(geom))::numeric, 3) FROM ankara.pois",
    sol("bbox_of", {"layer": "ankara.pois"}, "bbox.3"), tol=0.0015)
add("X02", "bbox", "What is the westernmost longitude of Turkey in world.countries? Round to 2 decimals.", "number",
    "SELECT round(ST_XMin(geom)::numeric, 2) FROM world.countries WHERE name = 'Turkey'",
    sol("bbox_of", {"layer": "world.countries", "filters": [f("name", "Turkey")]}, "bbox.0"), tol=0.01)

# ---------------------------------------------------------------- ambiguous: ask, do not guess
ASK = [
    ("Q01", "Find the pharmacies nearby."),
    ("Q02", "How many restaurants are within 500 metres?"),
    ("Q03", "Show me the nearest hospital."),
    ("Q04", "How many cafes are close to the park?"),
    ("Q05", "How many schools are within walking distance of the station?"),
    ("Q06", "Buffer the roads and tell me the area."),
    ("Q07", "How many people live in the neighbourhood?"),
    ("Q08", "List the biggest cities near the river."),
]
for qid, text in ASK:
    add(qid, "ambiguous", text, "clarify")
