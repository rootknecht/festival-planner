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

  // Markers are drawn shapes rather than image files, so there is nothing to
  // 404 and the car can take the colour of the car it stands for.
  function readableOutline(hex) {
    // A white car needs a dark outline and a black one a white outline, or
    // the marker disappears into the map on one of them.
    var value = hex.replace("#", "");
    if (value.length === 3) {
      value = value[0] + value[0] + value[1] + value[1] + value[2] + value[2];
    }
    var r = parseInt(value.slice(0, 2), 16) || 0;
    var g = parseInt(value.slice(2, 4), 16) || 0;
    var b = parseInt(value.slice(4, 6), 16) || 0;
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#15161a" : "#ffffff";
  }

  function carIcon(hex) {
    var outline = readableOutline(hex);
    return L.divIcon({
      className: "mapicon",
      iconSize: [34, 17],
      iconAnchor: [17, 9],
      html:
        '<svg width="34" height="17" viewBox="0 0 34 17" xmlns="http://www.w3.org/2000/svg">' +
        '<path d="M2 12.6h30.2V9.4c0-1.2-.9-2.2-2.1-2.4l-5.3-.9-3.2-2.9c-.7-.6-1.6-1-2.6-1h-8.6' +
        'c-1 0-2 .4-2.7 1.2L4.9 5.9 2.7 6.7C1.7 7.1 1.1 8 1.1 9v2.4c0 .7.4 1.2.9 1.2Z" fill="' +
        hex + '" stroke="' + outline + '" stroke-width="1.3" stroke-linejoin="round"/>' +
        '<path d="M9.4 3.4 7.2 6.1h7.3V3.4z" fill="' + outline + '" opacity="0.35"/>' +
        '<circle cx="9.6" cy="13.4" r="3.1" fill="#15161a" stroke="' + outline +
        '" stroke-width="1.1"/>' +
        '<circle cx="25.2" cy="13.4" r="3.1" fill="#15161a" stroke="' + outline +
        '" stroke-width="1.1"/></svg>'
    });
  }

  // The festival is the one place on the map that is a TARGET rather than a
  // waypoint, so it gets a crosshair. Drawn twice, white underneath, because
  // it has to stay legible on forest green and on motorway orange alike.
  function crosshairIcon() {
    var rings =
      '<circle cx="14" cy="14" r="7.5"/>' +
      '<path d="M14 1.5v5M14 21.5v5M1.5 14h5M21.5 14h5"/>';
    return L.divIcon({
      className: "mapicon",
      iconSize: [28, 28],
      iconAnchor: [14, 14],
      html:
        '<svg width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">' +
        '<g fill="none" stroke="#ffffff" stroke-width="5" stroke-linecap="round">' + rings + '</g>' +
        '<g fill="none" stroke="#15161a" stroke-width="2.4" stroke-linecap="round">' + rings + '</g>' +
        '<circle cx="14" cy="14" r="2.6" fill="#15161a"/></svg>'
    });
  }

  function offsetLatLngs(latlngs, pixels) {
    if (!pixels) return latlngs;
    var points = latlngs.map(function (pair) {
      return map.latLngToLayerPoint(L.latLng(pair[0], pair[1]));
    });
    // SIMPLIFY BEFORE OFFSETTING. A road geometry has its points ~0.2px
    // apart, and the direction between two such points is mostly rounding
    // noise — offsetting each one by its own normal turned a line into a
    // sawtooth several pixels wide. It also defeated Leaflet's own
    // simplification, so an offset line kept ~1150 screen points where the
    // un-offset one beside it kept 74, and looked twice as thick as a result.
    //
    // Leaflet simplifies every polyline anyway (smoothFactor); doing it here
    // first means the normals are taken between points far enough apart to
    // carry a real direction.
    // Tolerance 1 == Leaflet's own default smoothFactor, so an offset line
    // keeps the same level of detail as the un-offset line beside it.
    points = L.LineUtil.simplify(points, 1);
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
  var drawn = {};

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
      // The festival and a shared meeting point appear in EVERY car's point
      // list, so they were being drawn once per car — three markers and
      // three popups stacked on one spot, which went unnoticed while they
      // were identical circles. Shared kinds are drawn once.
      if (point.kind === "festival" || point.kind === "meeting") {
        var key = point.kind + ":" + point.lat + ":" + point.lon;
        if (drawn[key]) return;
        drawn[key] = true;
      }

      // Markers are NOT offset into lanes: a pickup is a place, and moving
      // it would be a lie. Three shapes, by what the point MEANS — where a
      // car sets off, where everybody gathers, and the target itself.
      var marker;
      if (point.kind === "festival") {
        marker = L.marker(latlng, { icon: crosshairIcon(), zIndexOffset: 400 });
      } else if (point.kind === "start") {
        marker = L.marker(latlng, { icon: carIcon(colour), zIndexOffset: 200 });
      } else {
        marker = L.circleMarker(latlng, {
          radius: point.kind === "meeting" ? 8 : 6,
          color: point.kind === "meeting" ? "#ffffff" : colour,
          fillColor: colour,
          fillOpacity: 0.9,
          weight: point.kind === "meeting" ? 3 : 2,
          dashArray: point.kind === "meeting" ? "3 3" : null
        });
      }
      marker.addTo(map).bindPopup(point.label);
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
