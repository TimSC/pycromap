"""The summary filter, SUMMARY_LUA, run with lupa.

Lua runtimes can't be shared between threads, so each thread has its own.
"""

import threading

import lupa
from django.conf import settings

_local = threading.local()


class SummaryFilter:
	def __init__(self, path):
		self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
		with open(path) as f:
			self.lua.execute(f.read())
		globals_ = self.lua.globals()
		self.functions = {kind: globals_[kind + "_filter"] for kind in ("node", "way", "relation")}
		missing = [kind for kind, function in self.functions.items() if function is None]
		if missing:
			raise ValueError("{} has no {}".format(path, ", ".join(k + "_filter" for k in missing)))

	def __call__(self, kind, tags, level, *extra):
		"""The tags to keep of an object, or None to leave it out."""
		result = self.functions[kind](self.lua.table_from(tags), level, *extra)
		if not result:
			return None
		return {str(k): str(v) for k, v in result.items()}


def summary_filter():
	"""This thread's summary filter."""
	path = str(settings.SUMMARY_LUA)
	if getattr(_local, "path", None) != path:
		_local.filter = SummaryFilter(path)
		_local.path = path
	return _local.filter
