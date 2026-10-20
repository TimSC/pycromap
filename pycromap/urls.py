from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
	path("admin/", admin.site.urls),
	path("", include("tiles.urls")),
]

if settings.DEBUG:
	# Cached tiles; in production the web server serves TILE_CACHE_DIR
	urlpatterns.append(re_path(r"^{}(?P<path>.*)$".format(settings.TILE_CACHE_URL.lstrip("/")),
		serve, {"document_root": settings.TILE_CACHE_DIR}))
