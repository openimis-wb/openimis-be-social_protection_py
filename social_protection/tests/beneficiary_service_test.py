import copy

from django.test import TestCase

from individual.models import Individual
from individual.tests.data import service_add_individual_payload

from social_protection.models import Beneficiary, BenefitPlan
from social_protection.services import BeneficiaryService
from social_protection.tests.data import (
    service_add_payload,
    service_beneficiary_add_payload,
    service_beneficiary_update_status_active_payload
)
from core.test_helpers import LogInHelper
from social_protection.tests.test_helpers import create_benefit_plan, create_individual


class BeneficiaryServiceTest(TestCase):
    user = None
    service = None
    query_all = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = BeneficiaryService(cls.user)
        cls.query_all = Beneficiary.objects.filter(is_deleted=False)
        cls.benefit_plan = create_benefit_plan(cls.user.username, payload_override={
            'code': 'SGQLTest',
            'type': "INDIVIDUAL",
            'max_beneficiaries': 1
        })

        cls.individual = create_individual(cls.user.username)
        cls.individual2 = create_individual(cls.user.username, payload_override={
            'first_name': "Second"
        })

    def _add_beneficiary_return_uuid(self, individual: Individual):
        payload = {
            **service_beneficiary_add_payload,
            "individual_id": individual.id,
            "benefit_plan_id": self.benefit_plan.id
        }
        result = self.service.create(payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        return result.get('data', {}).get('uuid')

    def test_add_beneficiary(self):
        uuid = self._add_beneficiary_return_uuid(self.individual)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)

    def test_update_beneficiary(self):
        def create_and_update_to_active(individual):
            uuid = self._add_beneficiary_return_uuid(individual)
            update_payload = {
                **service_beneficiary_update_status_active_payload,
                'id': uuid,
                'individual_id': individual.id,
                'benefit_plan_id': self.benefit_plan.id
            }
            return self.service.update(update_payload), uuid
        
        def check_individual_and_status(uuid, status):
            query = self.query_all.filter(uuid=uuid)
            self.assertEqual(query.count(), 1)
            self.assertEqual(query.first().status, status)
        
        def check_active_beneficiaries_at_max(msg):
            active_beneficiaries = self.query_all.filter(benefit_plan_id=self.benefit_plan.id, status="ACTIVE").distinct()
            self.assertEqual(active_beneficiaries.count(), self.benefit_plan.max_beneficiaries, msg)

        self.assertEqual(self.benefit_plan.max_beneficiaries, 1)

        result, uuid = create_and_update_to_active(self.individual)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        check_individual_and_status(uuid, "ACTIVE")
        check_active_beneficiaries_at_max("One active beneficiary should have been added")

        result, uuid = create_and_update_to_active(self.individual2)
        self.assertFalse(result.get('success', True), "Benefit plan's 'max active beneficiaries' was not enforced")
        check_individual_and_status(uuid, "POTENTIAL")
        check_active_beneficiaries_at_max("Second active beneficiary update should have been blocked")

    def test_delete_beneficiary(self):
        uuid = self._add_beneficiary_return_uuid(self.individual)
        delete_payload = {'id': uuid}
        result = self.service.delete(delete_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 0)
