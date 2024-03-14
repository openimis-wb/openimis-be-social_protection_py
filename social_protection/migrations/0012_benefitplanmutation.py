# Generated by Django 3.2.24 on 2024-03-05 11:28

import core.models
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_alter_interactiveuser_last_login_and_more'),
        ('social_protection', '0011_alter_beneficiary_date_created_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='BenefitPlanMutation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('benefit_plan', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='mutations', to='social_protection.benefitplan')),
                ('mutation', models.ForeignKey(on_delete=django.db.models.deletion.DO_NOTHING, related_name='benefit_plan', to='core.mutationlog')),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model, core.models.ObjectMutation),
        ),
    ]