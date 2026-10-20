class MapDataRouter:
	"""Keeps Django from managing the pycrocosm database: it is only read,
	through mapdata.reader, and nothing is migrated there."""

	def allow_migrate(self, db, app_label, model_name=None, **hints):
		if db == "pycrocosm":
			return False
		return None
