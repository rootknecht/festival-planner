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

  var map = L.map(node, { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var bounds = [];
  Object.keys(routes).forEach(function (carId) {
    var points = routes[carId] || [];
    var colour = node.getAttribute("data-color-" + carId) || "#c81f2a";
    var line = [];

    points.forEach(function (point) {
      var latlng = [point.lat, point.lon];
      line.push(latlng);
      bounds.push(latlng);
      // A meeting point is drawn as a ringed square-ish marker so it reads as
      // "everybody here" rather than as one more private pickup.
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
      L.polyline(road, { color: colour, weight: 4, opacity: 0.85 }).addTo(map);
      road.forEach(function (latlng) { bounds.push(latlng); });
    } else if (line.length > 1) {
      // No key configured, or the route could not be placed: straight dashed
      // connectors, which look like the sketch they are.
      L.polyline(line, { color: colour, weight: 3, opacity: 0.7, dashArray: "6 6" }).addTo(map);
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
        fillColor: "#5a6b8c",
        fillOpacity: 0.95,
        weight: 2
      })
        .addTo(map)
        .bindPopup(point.label);
    });
    if (line.length > 1) {
      L.polyline(line, {
        color: "#5a6b8c", weight: 3, opacity: 0.75, dashArray: "10 6"
      }).addTo(map);
    }
  });

  if (bounds.length === 0) {
    map.setView([51.0, 10.0], 5);
  } else if (bounds.length === 1) {
    map.setView(bounds[0], 11);
  } else {
    map.fitBounds(bounds, { padding: [30, 30] });
  }
})();
