from django.urls import path

from . import views

urlpatterns = [
	path("", views.index, name="index"),
	path("metadata", views.metadata, name="metadata"),
	path("style.json", views.style, name="style"),
	path("spec.json", views.tilejson, name="tilejson"),
	path("tiles/<int:zoom>/<int:x>/<int:y>.pbf", views.tile, name="tile"),
]
