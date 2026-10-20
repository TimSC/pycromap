from django.db import migrations, models


class Migration(migrations.Migration):

	dependencies = [
		("tiles", "0007_remove_tilestate_idle_remove_tilesweep_x"),
	]

	operations = [
		# A row is released, with a timestamp, once every tile in it is
		# rendered, rather than marked done (see tiles.idle._frontiers)
		migrations.RemoveIndex(model_name="tilesweep", name="tilesweep_claim"),
		migrations.RemoveField(model_name="tilesweep", name="done"),
		migrations.AddField(model_name="tilesweep", name="last_checked",
			field=models.DateTimeField(blank=True, null=True)),
		migrations.AddIndex(model_name="tilesweep",
			index=models.Index(fields=["claimed_by", "last_checked"], name="tilesweep_claim")),
	]
