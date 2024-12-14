# Generated by Django 4.2.16 on 2024-12-14 14:16

from django.db import migrations

def max_beneficiaries_replace_zeros_with_nulls(apps, schema_editor):
    BenefitPlan = apps.get_model("social_protection", "BenefitPlan")
    for benefit_plan in BenefitPlan.objects.filter(max_beneficiaries=0):
        benefit_plan.max_beneficiaries = None
        benefit_plan.save()
    
def max_beneficiaries_replace_nulls_with_zeros(apps, schema_editor):
    BenefitPlan = apps.get_model("social_protection", "BenefitPlan")
    for benefit_plan in BenefitPlan.objects.filter(max_beneficiaries__isnull=True):
        benefit_plan.max_beneficiaries = 0
        benefit_plan.save()


class Migration(migrations.Migration):

    dependencies = [
        ('social_protection', '0013_alter_benefitplan_max_beneficiaries_and_more'),
    ]

    operations = [
        migrations.RunPython(
            max_beneficiaries_replace_zeros_with_nulls,
            max_beneficiaries_replace_nulls_with_zeros
        )
    ]