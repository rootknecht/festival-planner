/* Car-sharing map: OpenStreetMap tiles, one coloured line per car.
   Markers are drawn shapes, not image files, so there is nothing to 404. */
(function () {
  "use strict";

  var node = document.getElementById("map");
  if (!node || typeof L === "undefined") return;

  var routes = {};
  try {
    routes = JSON.parse(node.getAttribute("data-routes") || "{}");
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
      L.circleMarker(latlng, {
        radius: point.kind === "festival" ? 9 : 6,
        color: point.kind === "festival" ? "#ffffff" : colour,
        fillColor: colour,
        fillOpacity: 0.9,
        weight: 2
      })
        .addTo(map)
        .bindPopup(point.label);
    });

    if (line.length > 1) {
      // Straight connectors, not road routes: the public routing demo server
      // may not be used by applications, and a self-hosted engine is a later
      // upgrade.
      L.polyline(line, { color: colour, weight: 3, opacity: 0.7, dashArray: "6 6" }).addTo(map);
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
