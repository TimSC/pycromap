from django.db import migrations


class Migration(migrations.Migration):

	dependencies = [
		("tiles", "0006_tilesweep"),
	]

	operations = [
		# Idle work is marked by a negative priority (see tiles.idle), and a
		# sweep row's frontier is rechecked on the claim (tiles.idle._frontier)
		migrations.RemoveField(model_name="tilestate", name="idle"),
		migrations.RemoveField(model_name="tilesweep", name="x"),
	]
