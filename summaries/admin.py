from django.contrib import admin

from .models import Recipe, TileSummary


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
	list_display = ("id", "hash", "created_at")
	ordering = ("-created_at",)


@admin.register(TileSummary)
class TileSummaryAdmin(admin.ModelAdmin):
	list_display = ("zoom", "x", "y", "version", "recipe_id", "built_at", "nodes", "ways", "relations")
	list_filter = ("zoom",)
	exclude = ("data",)
	ordering = ("-built_at",)
