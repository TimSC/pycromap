from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path, re_path

from tiles.views import cached_tile

urlpatterns = [
	path("admin/", admin.site.urls),
	path("", include("tiles.urls")),
]

if settings.DEBUG:
	# Static files, when not under runserver (which serves them itself), and
	# cached tiles; in production the web server serves both
	urlpatterns += staticfiles_urlpatterns()
	urlpatterns.append(re_path(r"^{}(?P<path>.*)$".format(settings.TILE_CACHE_URL.lstrip("/")),
		cached_tile))
