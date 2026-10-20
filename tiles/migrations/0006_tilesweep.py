from django.db import migrations, models


class Migration(migrations.Migration):

	dependencies = [
		("tiles", "0005_idle_jobs"),
	]

	operations = [
		migrations.CreateModel(
			name="TileSweep",
			fields=[
				("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
				("kind", models.CharField(choices=[("tile", "tile"), ("summary", "summary")], default="tile", max_length=16)),
				("zoom", models.PositiveSmallIntegerField()),
				("y", models.PositiveIntegerField()),
				("x", models.PositiveIntegerField(default=0)),
				("claimed_by", models.CharField(blank=True, max_length=100)),
				("claimed_at", models.DateTimeField(blank=True, null=True)),
				("done", models.BooleanField(default=False)),
			],
			options={
				"constraints": [
					models.UniqueConstraint(fields=["kind", "zoom", "y"], name="tilesweep_unique_row"),
				],
				"indexes": [
					models.Index(fields=["done", "claimed_by"], name="tilesweep_claim"),
				],
			},
		),
	]
