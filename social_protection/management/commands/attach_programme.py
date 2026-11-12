from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import User
from social_protection.models import BenefitPlan, Programme, UserProgramme


class Command(BaseCommand):
    help = (
        "Creates a programme (or gets the live one with the same code) and optionally "
        "attaches every benefit plan without programme to it and adds every active "
        "interactive user as a member. Dry run unless --apply is given; running it "
        "again changes nothing already done."
    )

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True, help="Programme code")
        parser.add_argument("--name", help="Programme name, required when the programme does not exist yet")
        parser.add_argument("--description", default=None, help="Programme description, used on creation")
        parser.add_argument(
            "--attach-unassigned-benefit-plans", action="store_true",
            help="Attach every benefit plan without programme, deleted ones included",
        )
        parser.add_argument(
            "--add-active-users", action="store_true",
            help="Add every active interactive user as a member",
        )
        parser.add_argument("--username", help="User recorded as author of the changes, required with --apply")
        parser.add_argument("--apply", action="store_true", help="Write the changes")

    def handle(self, *args, **options):
        apply = options["apply"]
        code = options["code"].strip()
        if not code:
            raise CommandError("--code must not be empty")
        author = None
        if apply:
            if not options.get("username"):
                raise CommandError("--username is required with --apply")
            author = User.objects.filter(username=options["username"]).first()
            if author is None:
                raise CommandError(f"Unknown user: {options['username']}")

        with transaction.atomic():
            programme = self._get_or_create_programme(code, options, author, apply)
            if options["attach_unassigned_benefit_plans"]:
                self._attach_benefit_plans(programme, author, apply)
            if options["add_active_users"]:
                self._add_members(programme, author, apply)

        if not apply:
            self.stdout.write("Dry run: nothing written. Add --apply to write.")

    def _get_or_create_programme(self, code, options, author, apply):
        programme = Programme.objects.filter(code=code, is_deleted=False).first()
        if programme:
            self.stdout.write(f"Programme {code}: existing ({programme.id})")
            return programme
        name = (options.get("name") or "").strip()
        if not name:
            raise CommandError(f"Programme {code} does not exist: --name is required to create it")
        if not apply:
            self.stdout.write(f"Programme {code}: would be created")
            return None
        programme = Programme(code=code, name=name, description=options.get("description"))
        programme.save(user=author)
        self.stdout.write(f"Programme {code}: created ({programme.id})")
        return programme

    def _attach_benefit_plans(self, programme, author, apply):
        benefit_plans = list(BenefitPlan.objects.filter(programme__isnull=True))
        deleted = sum(1 for benefit_plan in benefit_plans if benefit_plan.is_deleted)
        verb = "attached" if apply else "to attach"
        self.stdout.write(
            f"Benefit plans without programme {verb}: {len(benefit_plans)} "
            f"({len(benefit_plans) - deleted} live, {deleted} deleted)"
        )
        if not apply:
            return
        for benefit_plan in benefit_plans:
            benefit_plan.programme = programme
            benefit_plan.save(user=author)

    def _add_members(self, programme, author, apply):
        users = set(
            User.objects.filter(
                i_user__isnull=False, i_user__validity_to__isnull=True,
            ).values_list("id", flat=True)
        )
        members = set()
        if programme is not None:
            members = set(
                UserProgramme.objects.filter(
                    programme=programme, is_deleted=False, user_id__in=users,
                ).values_list("user_id", flat=True)
            )
        to_add = users - members
        verb = "added" if apply else "to add"
        self.stdout.write(
            f"Active interactive users: {len(users)}, already members: {len(members)}, {verb}: {len(to_add)}"
        )
        if not apply or not to_add:
            return
        UserProgramme.bulk_save(
            [{"user_id": user_id, "programme_id": programme.id} for user_id in to_add],
            author,
        )
