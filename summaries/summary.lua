-- What a tile summary keeps, at each level of the summary pyramid.
--
-- Each function gets an object's tags (a table) and the summary level (12,
-- 10, 8 or 6), and returns the tags to keep (a table), or nil to leave the
-- object out. Ways also get whether they are closed (a ring).
--
-- A summary at a level is used to render the zooms below it (level 12 for
-- z11, level 10 for z10 and z9, level 8 for z8 and z7, level 6 for z6; see
-- TILE_ZOOMS), so it keeps what the profile (profile/process.lua) draws
-- there, with the tags the profile reads.
--
-- Ways in a relation that is kept are kept too, so that it can be built, and
-- so are the nodes kept ways and relations need.

local function set(list)
	local s = {}
	for _, v in ipairs(list) do s[v] = true end
	return s
end

-- The least important roads kept at each level
local roads = {
	[12] = set { "motorway", "trunk", "primary", "secondary", "tertiary",
		"motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "busway" },
	[10] = set { "motorway", "trunk", "primary", "secondary",
		"motorway_link", "trunk_link", "primary_link", "secondary_link" },
	[8] = set { "motorway", "trunk", "primary" },
	[6] = set { "motorway", "trunk" },
}

local places = {
	[12] = set { "continent", "country", "state", "province", "city", "town", "village",
		"borough", "suburb" },
	[10] = set { "continent", "country", "state", "province", "city", "town", "village" },
	[8] = set { "continent", "country", "state", "province", "city", "town" },
	[6] = set { "continent", "country", "state", "province", "city" },
}

-- The lowest admin_level kept at each level
local admin_levels = { [12] = 8, [10] = 6, [8] = 4, [6] = 4 }

local landuse = set { "residential", "commercial", "industrial", "retail", "railway",
	"cemetery", "military", "quarry", "forest", "farmland", "meadow", "grass",
	"orchard", "vineyard", "allotments", "recreation_ground", "village_green" }
local natural = set { "water", "wood", "scrub", "grassland", "heath", "wetland",
	"beach", "sand", "bare_rock", "scree", "glacier", "coastline", "bay" }
local leisure = set { "park", "nature_reserve", "golf_course", "garden" }
local waterways = { [12] = set { "river", "canal" }, [10] = set { "river", "canal" },
	[8] = set { "river" }, [6] = set { "river" } }

-- Tags tilemaker's profile reads for what a summary keeps
local kept_keys = set { "highway", "railway", "service", "waterway", "natural", "water",
	"landuse", "leisure", "boundary", "admin_level", "maritime", "disputed", "place",
	"population", "capital", "type", "name", "name:en", "name:latin", "int_name",
	"ref", "network", "bridge", "tunnel", "layer", "oneway", "toll", "expressway",
	"access", "intermittent", "surface", "protection_title", "wetland", "aeroway", "iata", "icao" }

local function keep(tags)
	local out = {}
	for k, v in pairs(tags) do
		if kept_keys[k] or string.sub(k, 1, 5) == "name:" then out[k] = v end
	end
	return out
end

local function admin_boundary(tags, level)
	return tags.boundary == "administrative" and (tonumber(tags.admin_level) or 11) <= admin_levels[level]
end

local function area(tags)
	return landuse[tags.landuse] or natural[tags.natural] or leisure[tags.leisure]
		or tags.aeroway == "aerodrome"
end

function node_filter(tags, level)
	if tags.place and places[level][tags.place] then return keep(tags) end
	return nil
end

function way_filter(tags, level, closed)
	if tags.highway and roads[level][tags.highway] then return keep(tags) end
	-- Main lines only
	if tags.railway == "rail" and tags.service == nil then return keep(tags) end
	if tags.waterway and waterways[level][tags.waterway] then return keep(tags) end
	if tags.natural == "coastline" then return keep(tags) end
	if admin_boundary(tags, level) then return keep(tags) end
	if closed and area(tags) then return keep(tags) end
	return nil
end

function relation_filter(tags, level)
	if tags.type == "multipolygon" and area(tags) then return keep(tags) end
	if tags.type == "boundary" and admin_boundary(tags, level) then return keep(tags) end
	return nil
end
