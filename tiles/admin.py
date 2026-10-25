"""The admin: the requests, the jobs in the render queue, the rendered
tiles, and the stored summaries. The renderers own the jobs, so nothing there may be added,
changed or deleted."""

from django.contrib import admin
from django.db.models import Count

from .models import Job, Tile, TileRequest
from . import queue


@admin.register(TileRequest)
class TileRequestAdmin(admin.ModelAdmin):
	"""The requests being answered, and those whose clients went away (kept
	CANCELLED_REQUEST_KEEP seconds)."""
	list_display = ("__str__", "started_at", "status", "claimed_by", "cache_redirect", "cancelled", "cancelled_at")
	list_filter = ("status", "cache_redirect", "cancelled", "zoom")
	ordering = ("-started_at",)
	readonly_fields = ("zoom", "x", "y", "started_at", "status", "claimed_by", "claimed_at", "attempts",
		"cache_redirect", "cancelled", "cancelled_at")
	fields = readonly_fields
	actions = None

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
	change_list_template = "job_change_list.html"
	list_display = ("job", "status", "queued", "claimed", "tries", "error")
	list_filter = ("kind", "status", "zoom")
	search_fields = ("error",)
	# The jobs to watch first: rendering, then queued in the order they will
	# be taken, then failed
	ordering = ("-status", "-priority", "queued_at")
	readonly_fields = ("kind", "zoom", "x", "y", "status", "idle", "priority", "queued_at",
		"claimed_by", "claimed_at", "attempts", "error")
	fields = readonly_fields
	actions = None

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False

	# The columns

	@admin.display(description="job")
	def job(self, job):
		return "{} {}/{}/{}{}".format(job.kind, job.zoom, job.x, job.y, " (idle)" if job.idle else "")

	@admin.display(description="queued")
	def queued(self, job):
		return queue.span(job.queued_at)

	@admin.display(description="claimed")
	def claimed(self, job):
		if not job.claimed_by:
			return ""
		return "{} {}".format(job.claimed_by, queue.span(job.claimed_at)).strip()

	@admin.display(description="tries")
	def tries(self, job):
		return job.attempts or ""

	# The summary above the list

	def changelist_view(self, request, extra_context=None):
		extra_context = {**(extra_context or {}), "job_counts": self._counts()}
		return super().changelist_view(request, extra_context=extra_context)

	@staticmethod
	def _counts():
		by_status = {row["status"]: row["n"] for row in
			Job.objects.values("status").annotate(n=Count("pk"))}
		return [
			("rendering", by_status.get(Job.RENDERING, 0)),
			("queued", by_status.get(Job.QUEUED, 0)),
			("idle queued", Job.objects.filter(status=Job.QUEUED, idle=True).count()),
			("failed", by_status.get(Job.FAILED, 0)),
		]


@admin.register(Tile)
class TileAdmin(admin.ModelAdmin):
	"""The rendered tiles. Ordered by key: by zoom, row and column."""
	list_display = ("__str__", "rendered_at", "data_version", "render_seconds", "file")
	list_filter = ("zoom",)
	ordering = ("id",)
	readonly_fields = ("zoom", "x", "y", "rendered_at", "data_version", "edits_checked_at", "render_seconds", "file")
	fields = readonly_fields
	actions = None

	def has_add_permission(self, request):
		return False

	def has_delete_permission(self, request, obj=None):
		return False
