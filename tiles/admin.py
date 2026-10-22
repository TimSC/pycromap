"""The admin: the state of the jobs in the render queue, and the stored
summaries. The renderers own the jobs, so nothing there may be added,
changed or deleted."""

from django.contrib import admin
from django.db.models import Count

from .models import TileRequest, TileState
from . import queue


@admin.register(TileRequest)
class TileRequestAdmin(admin.ModelAdmin):
	"""The requests being answered, and those whose clients went away (kept
	CANCELLED_REQUEST_KEEP seconds)."""
	list_display = ("__str__", "started_at", "status", "claimed_by", "cache_redirect", "cancelled", "cancelled_at")
	list_filter = ("status", "cache_redirect", "cancelled", "zoom")
	ordering = ("-started_at",)
	readonly_fields = ("zoom", "x", "y", "started_at", "status", "claimed_by", "claimed_at", "attempts",
		"cache_redirect", "dependencies", "cancelled", "cancelled_at")
	fields = readonly_fields
	actions = None

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(TileState)
class TileStateAdmin(admin.ModelAdmin):
	change_list_template = "tilestate_change_list.html"
	list_display = ("job", "status", "requested", "queued", "claimed", "render", "error")
	list_filter = ("kind", "status", "zoom")
	search_fields = ("error",)
	# The jobs to watch first: rendering, then queued in the order they will
	# be taken, then failed, then done
	ordering = ("-status", "-priority", "queued_at")
	readonly_fields = ("kind", "zoom", "x", "y", "status", "priority",
		"queued_at", "requested_at", "request_count",
		"claimed_by", "claimed_at", "attempts", "error", "rendered_at", "data_version",
		"render_seconds", "file")
	fields = readonly_fields
	actions = None

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False

	# The columns

	@admin.display(description="job")
	def job(self, tile):
		markers = []
		if tile.priority < 0:
			markers.append("idle")
		return "{} {}/{}/{}{}".format(tile.kind, tile.zoom, tile.x, tile.y,
			" ({})".format(", ".join(markers)) if markers else "")

	@admin.display(description="requested")
	def requested(self, tile):
		if not tile.request_count:
			return ""
		return "{}x {}".format(tile.request_count, queue.span(tile.requested_at))

	@admin.display(description="queued")
	def queued(self, tile):
		return queue.span(tile.queued_at)

	@admin.display(description="claimed")
	def claimed(self, tile):
		if not tile.claimed_by:
			return ""
		return "{} {}".format(tile.claimed_by, queue.span(tile.claimed_at)).strip()

	@admin.display(description="render")
	def render(self, tile):
		parts = []
		if tile.render_seconds is not None:
			parts.append("{:.2f}s".format(tile.render_seconds))
		if tile.data_version is not None:
			parts.append("v{}".format(tile.data_version))
		if tile.file:
			parts.append("file")
		if tile.attempts and tile.status == TileState.FAILED:
			parts.append("{} tries".format(tile.attempts))
		return " ".join(parts)

	# The summary above the list

	def changelist_view(self, request, extra_context=None):
		extra_context = {**(extra_context or {}), "job_counts": self._counts()}
		return super().changelist_view(request, extra_context=extra_context)

	@staticmethod
	def _counts():
		by_status = {row["status"]: row["n"] for row in
			TileState.objects.values("status").annotate(n=Count("pk"))}
		return [
			("rendering", by_status.get(TileState.RENDERING, 0)),
			("queued", by_status.get(TileState.QUEUED, 0)),
			("idle queued", TileState.objects.filter(status=TileState.QUEUED, priority__lt=0).count()),
			("failed", by_status.get(TileState.FAILED, 0)),
			("done", by_status.get(TileState.DONE, 0)),
		]
