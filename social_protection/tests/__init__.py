# flake8: noqa
from .beneficiary_service_test import *
from .benefit_plan_service_test import BenefitPlanServiceTest
from .group_beneficiary_service_test import *
from .beneficiary_import_service_test import BeneficiaryImportServiceTest
from .beneficiary_gql_test import BeneficiaryGQLTest
from .group_beneficiary_gql_test import GroupBeneficiaryGQLTest
from .test_workflows_beneficiaries_upload import ProcessImportBeneficiariesWorkflowTest
from .test_workflows_beneficiaries_update import ProcessUpdateBeneficiariesWorkflowTest
from .activity_gql_test import ActivitiesGQLTest
from .project_gql_test import ProjectsGQLTest
from .programme_scope_test import (
    ProgrammeScopeTest, ZeroProgrammeDeploymentTest, ProgrammeModelScopeTest, ProgrammeRightsTest,
)
from .programme_gql_test import ProgrammeGQLTest
from .programme_type_scope_test import ProgrammeTypeScopeTest
from .programme_write_scope_test import (
    ProgrammeWriteScopeTest, ProgrammeFlowTaskTest, ProgrammeRestScopeTest, ZeroProgrammeWriteCostTest,
)
# TODO: implement group upload workflow
# from .group_import_gql_test import GroupBeneficiaryImportGQLTest
