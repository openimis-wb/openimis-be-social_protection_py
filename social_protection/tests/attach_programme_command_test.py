from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from core.models import User
from social_protection.models import BenefitPlan, Programme, UserProgramme
from social_protection.tests.programme_helpers import (
    create_programme,
    create_programme_benefit_plan,
    create_user_with_rights,
    get_admin_user,
)


class AttachProgrammeCommandTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_admin_user()
        username = cls.admin.username
        cls.other_programme = create_programme("APOTHER", username)
        cls.attached_elsewhere = create_programme_benefit_plan("APELSE", cls.other_programme, username)
        cls.unassigned = create_programme_benefit_plan("APNONE", None, username)
        cls.unassigned_deleted = create_programme_benefit_plan("APDEL", None, username, is_deleted=True)
        cls.user = create_user_with_rights("apUser")

    def _run(self, *extra):
        out = StringIO()
        call_command(
            "attach_programme", "--code", "APMAIN", "--name", "Main programme",
            "--attach-unassigned-benefit-plans", "--add-active-users", *extra, stdout=out,
        )
        return out.getvalue()

    def _active_user_count(self):
        return User.objects.filter(i_user__isnull=False, i_user__validity_to__isnull=True).count()

    def test_dry_run_writes_nothing(self):
        plans_without_programme = BenefitPlan.objects.filter(programme__isnull=True).count()
        for _ in range(2):
            output = self._run()
            self.assertIn("Programme APMAIN: would be created", output)
            self.assertIn(f"Benefit plans without programme to attach: {plans_without_programme}", output)
            self.assertIn(f"to add: {self._active_user_count()}", output)
            self.assertIn("Dry run: nothing written", output)
        self.assertFalse(Programme.objects.filter(code="APMAIN").exists())
        self.assertIsNone(BenefitPlan.objects.get(id=self.unassigned.id).programme_id)

    def test_apply_attaches_plans_and_members_and_is_idempotent(self):
        plans_without_programme = BenefitPlan.objects.filter(programme__isnull=True).count()
        output = self._run("--apply", "--username", self.admin.username)
        programme = Programme.objects.get(code="APMAIN", is_deleted=False)
        self.assertIn(f"Programme APMAIN: created ({programme.id})", output)
        self.assertIn(f"Benefit plans without programme attached: {plans_without_programme}", output)
        self.assertEqual(BenefitPlan.objects.get(id=self.unassigned.id).programme_id, programme.id)
        self.assertEqual(BenefitPlan.objects.get(id=self.unassigned_deleted.id).programme_id, programme.id)
        self.assertEqual(BenefitPlan.objects.get(id=self.attached_elsewhere.id).programme_id,
                         self.other_programme.id)
        self.assertTrue(UserProgramme.objects.filter(
            user_id=self.user.id, programme=programme, is_deleted=False).exists())
        members = UserProgramme.objects.filter(programme=programme, is_deleted=False).count()
        self.assertEqual(members, self._active_user_count())

        output = self._run("--apply", "--username", self.admin.username)
        self.assertIn(f"Programme APMAIN: existing ({programme.id})", output)
        self.assertIn("Benefit plans without programme attached: 0", output)
        self.assertIn(f"already members: {members}, added: 0", output)
        self.assertEqual(Programme.objects.filter(code="APMAIN").count(), 1)
        self.assertEqual(UserProgramme.objects.filter(programme=programme, is_deleted=False).count(), members)

    def test_apply_requires_an_author(self):
        with self.assertRaises(CommandError):
            self._run("--apply")
        with self.assertRaises(CommandError):
            self._run("--apply", "--username", "nobody-with-this-name")
