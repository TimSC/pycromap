from django.contrib import admin

from .models import TileState


@admin.register(TileState)
class TileStateAdmin(admin.ModelAdmin):
	list_display = ("zoom", "x", "y", "status", "requested_at", "request_count",
		"rendered_at", "data_version", "render_seconds", "attempts")
	list_filter = ("status", "zoom")
	search_fields = ("error",)
	ordering = ("-requested_at",)
