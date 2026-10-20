from django.contrib import admin

from .models import TileSummary


@admin.register(TileSummary)
class TileSummaryAdmin(admin.ModelAdmin):
	list_display = ("zoom", "x", "y", "version", "built_at", "nodes", "ways", "relations")
	list_filter = ("zoom",)
	exclude = ("data",)
	ordering = ("-built_at",)
