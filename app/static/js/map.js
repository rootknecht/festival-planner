/* Car-sharing map: OpenStreetMap tiles, one coloured line per car.
   Markers are drawn shapes, not image files, so there is nothing to 404. */
(function () {
  "use strict";

  var node = document.getElementById("map");
  if (!node || typeof L === "undefined") return;

  var routes = {};
  var roads = {};
  var trains = {};
  try {
    routes = JSON.parse(node.getAttribute("data-routes") || "{}");
    // Road geometry, present only for cars the routing service could place.
    roads = JSON.parse(node.getAttribute("data-roads") || "{}");
    trains = JSON.parse(node.getAttribute("data-trains") || "{}");
  } catch (e) {
    return;
  }

  // Two cars from the same town to the same festival share every metre of
  // their line, so the one drawn last hid the other completely and the legend
  // promised a colour the map never showed. Each line is therefore shifted a
  // few PIXELS sideways from the true route, half the group to each side, so
  // a shared stretch reads as a ribbon of colours running in parallel.
  //
  // Pixels, not degrees: a metre offset would vanish at country zoom and
  // swamp the street at close zoom. That means the offsets must be recomputed
  // whenever the scale changes — see the zoomend handler at the end.
  var SPACING_PX = 4;
  var tracked = [];

  function offsetLatLngs(latlngs, pixels) {
    if (!pixels) return latlngs;
    var points = latlngs.map(function (pair) {
      return map.latLngToLayerPoint(L.latLng(pair[0], pair[1]));
    });
    return points.map(function (point, index) {
      // The normal at a vertex follows the direction of travel THROUGH it, so
      // corners bend outwards instead of breaking the ribbon apart.
      var before = points[index === 0 ? 0 : index - 1];
      var after = points[index === points.length - 1 ? index : index + 1];
      var dx = after.x - before.x;
      var dy = after.y - before.y;
      var length = Math.sqrt(dx * dx + dy * dy);
      if (!length) return map.layerPointToLatLng(point);
      return map.layerPointToLatLng(
        L.point(point.x + (-dy / length) * pixels, point.y + (dx / length) * pixels)
      );
    });
  }

  function drawOffset(latlngs, pixels, style) {
    // Drawn on the TRUE route first and shifted later, because converting a
    // coordinate to a pixel needs a map that already has a centre and zoom —
    // and that is only set at the end, once the bounds of everything are
    // known. Offsetting here threw "Set map center and zoom first." and the
    // map came up with no lines at all.
    var line = L.polyline(latlngs, style).addTo(map);
    if (pixels) tracked.push({ line: line, source: latlngs, pixels: pixels });
    return line;
  }

  function applyOffsets() {
    tracked.forEach(function (entry) {
      entry.line.setLatLngs(offsetLatLngs(entry.source, entry.pixels));
    });
  }

  var map = L.map(node, { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var trainColour = node.getAttribute("data-train-color") || "#5a6b8c";
  var bounds = [];

  // Count every line that will be drawn BEFORE drawing any of them: a line's
  // lane depends on how many neighbours it has, and the group is centred on
  // the true route so a single car is not nudged off the road it drives.
  var lanes = [];
  Object.keys(routes).forEach(function (carId) {
    var points = routes[carId] || [];
    var road = roads[carId];
    if ((road && road.length > 1) || points.length > 1) {
      lanes.push("car:" + carId);
    }
  });
  Object.keys(trains).forEach(function (trainId) {
    if ((trains[trainId] || []).length > 1) lanes.push("train:" + trainId);
  });

  function laneOffset(id) {
    if (lanes.length < 2) return 0;
    var index = lanes.indexOf(id);
    if (index < 0) return 0;
    return (index - (lanes.length - 1) / 2) * SPACING_PX;
  }

  Object.keys(routes).forEach(function (carId) {
    var points = routes[carId] || [];
    var colour = node.getAttribute("data-color-" + carId) || "#c81f2a";
    var pixels = laneOffset("car:" + carId);
    var line = [];

    points.forEach(function (point) {
      var latlng = [point.lat, point.lon];
      line.push(latlng);
      bounds.push(latlng);
      // A meeting point is drawn as a ringed square-ish marker so it reads as
      // "everybody here" rather than as one more private pickup. Markers are
      // NOT offset: a pickup is a place, and moving it would be a lie.
      L.circleMarker(latlng, {
        radius: point.kind === "festival" ? 9 : (point.kind === "meeting" ? 8 : 6),
        color: (point.kind === "festival" || point.kind === "meeting") ? "#ffffff" : colour,
        fillColor: colour,
        fillOpacity: 0.9,
        weight: point.kind === "meeting" ? 3 : 2,
        dashArray: point.kind === "meeting" ? "3 3" : null
      })
        .addTo(map)
        .bindPopup(point.label);
    });

    var road = roads[carId];
    if (road && road.length > 1) {
      // A real road route is drawn SOLID. The dashed line below means "we are
      // guessing"; keeping the same style for both would hide which is which.
      drawOffset(road, pixels, { color: colour, weight: 4, opacity: 0.85 });
      road.forEach(function (latlng) { bounds.push(latlng); });
    } else if (line.length > 1) {
      // No key configured, or the route could not be placed: straight dashed
      // connectors, which look like the sketch they are.
      drawOffset(line, pixels, {
        color: colour, weight: 3, opacity: 0.7, dashArray: "6 6"
      });
    }
  });

  // Trains: station to change to station. Deliberately dashed in a neutral
  // colour — rail geometry is not road geometry and this line is a corridor,
  // not a track. One style for "this is approximate", one for "this is the
  // actual way", and a train is always the former.
  Object.keys(trains).forEach(function (trainId) {
    var points = trains[trainId] || [];
    var line = [];
    points.forEach(function (point) {
      var latlng = [point.lat, point.lon];
      line.push(latlng);
      bounds.push(latlng);
      L.circleMarker(latlng, {
        radius: point.kind === "change" ? 6 : 7,
        color: "#ffffff",
        fillColor: trainColour,
        fillOpacity: 0.95,
        weight: 2
      })
        .addTo(map)
        .bindPopup(point.label);
    });
    if (line.length > 1) {
      drawOffset(line, laneOffset("train:" + trainId), {
        color: trainColour, weight: 3, opacity: 0.75, dashArray: "10 6"
      });
    }
  });

  if (bounds.length === 0) {
    map.setView([51.0, 10.0], 5);
  } else if (bounds.length === 1) {
    map.setView(bounds[0], 11);
  } else {
    map.fitBounds(bounds, { padding: [30, 30] });
  }

  // Only now does a pixel exist to offset by. And since the offset is in
  // pixels, it has to be recomputed whenever the scale changes, or the lanes
  // drift apart as you zoom out and collapse into one as you zoom in.
  if (tracked.length) {
    applyOffsets();
    map.on("zoomend", applyOffsets);
  }
})();
