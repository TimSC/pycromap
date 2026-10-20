from django.apps import AppConfig


class TilesConfig(AppConfig):
	default_auto_field = "django.db.models.BigAutoField"
	name = "tiles"

	def ready(self):
		# So that cached tiles are served as vector tiles
		import mimetypes
		mimetypes.add_type("application/vnd.mapbox-vector-tile", ".pbf")
