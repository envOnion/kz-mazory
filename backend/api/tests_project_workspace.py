from unittest.mock import patch
from decimal import Decimal
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .models import Company, Project, Team, TeamMembership, FinancialRecord, RawMessage, MessageProcessingTrace, FactCandidate


@patch('api.views.PageNumberPagination.page_size', 2)
@override_settings(ALLOWED_HOSTS=['testserver'], REST_FRAMEWORK={'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination', 'PAGE_SIZE': 2})
class ProjectWorkspaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('workspace')
        self.team = Team.objects.create(name='Sales')
        TeamMembership.objects.create(user=self.user, team=self.team, role='finance', status='active')
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def project(self, name, **kwargs):
        return Project.objects.create(name=name, team=self.team, identity_confirmed=True, **kwargs)

    def payment(self, p, amount):
        return FinancialRecord.objects.create(project=p, amount=amount, payment_date=timezone.localdate(), status='received', is_verified=True)

    def test_sort_and_paginate_all_data_before_missing_projects(self):
        for name in ['A empty', 'B empty', 'C empty']:
            self.project(name)
        p = self.project('Z paid')
        self.payment(p, 117000000)
        r = self.api.get('/api/projects/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['results'][0]['id'], p.id)
        self.assertEqual(r.data['group_counts'], {'with_data': 1, 'without_data': 3})
        row = r.data['results'][0]
        self.assertEqual(row['paid_amount'], '117000000.00')
        self.assertFalse(row['contract_known'])
        self.assertFalse(row['balance_known'])
        self.assertEqual(row['data_completeness'], 'partial')
        self.assertIsNotNone(r.data['next'])
        r2 = self.api.get('/api/projects/', {'page': 2})
        self.assertEqual(len(r2.data['results']), 2)

    def test_registered_zero_is_data_and_known_balance_stays_zero(self):
        p = self.project('Refund', contract_amount=Decimal('100'))
        self.payment(p, 100)
        r = self.api.get('/api/projects/', {'group': 'with_data', 'completeness': 'complete'})
        self.assertTrue(r.data['results'][0]['balance_known'])
        self.assertEqual(r.data['results'][0]['due_amount'], '0.00')
        self.payment(p, -100)
        p.contract_amount = 0
        p.save()
        r = self.api.get('/api/projects/', {'group': 'with_data'})
        self.assertEqual(r.data['count'], 1)
        self.assertTrue(r.data['results'][0]['payments_known'])
        self.assertEqual(r.data['results'][0]['paid_amount'], '0.00')

    def test_filters_company_and_data_state_and_access(self):
        company = Company.objects.create(name='Top Build')
        p = self.project('ЦТП 343', company=company, status='in_execution')
        self.payment(p, 10)
        self.project('Empty')
        other = Team.objects.create(name='Other')
        Project.objects.create(name='Secret', team=other, identity_confirmed=True, contract_amount=100)
        r = self.api.get('/api/projects/', {'search': 'Top', 'stage': 'in_execution', 'group': 'with_data'})
        self.assertEqual([row['id'] for row in r.data['results']], [p.id])
        self.assertEqual(r.data['group_counts']['without_data'], 0)
        self.assertEqual(self.api.get('/api/projects/', {'team_id': other.id}).data['count'], 0)
        self.assertEqual(self.api.get('/api/projects/', {'group': 'without_data', 'completeness': 'complete'}).data['count'], 0)
        self.assertEqual(self.api.get('/api/projects/', {'stage': 'bad'}).status_code, 400)

    def test_reasons_do_not_leak_inaccessible_sources(self):
        p = self.project('Missing')
        raw = RawMessage.objects.create(source='manual', team=self.team, project=p, message_id='linked', timestamp=timezone.now(), content='Сумма договора')
        trace = MessageProcessingTrace.objects.create(raw_message=raw)
        FactCandidate.objects.create(project=p, team=self.team, trace=trace, source_key='linked', fact_type='project', status='rejected', proposed_changes={'contract_amount': '100'}, review_reason='Это сумма КП')
        hidden = RawMessage.objects.create(source='waha', team=self.team, message_id='hidden', timestamp=timezone.now(), content='secret')
        trace2 = MessageProcessingTrace.objects.create(raw_message=hidden)
        FactCandidate.objects.create(project=p, team=self.team, trace=trace2, source_key='hidden', fact_type='project', status='rejected', proposed_changes={'contract_amount': '100'}, review_reason='SECRET')
        row = self.api.get('/api/projects/').data['results'][0]
        messages = [r['message'] for r in row['missing_data_reasons']]
        self.assertTrue(any('Это сумма КП' in m for m in messages))
        self.assertFalse(any('SECRET' in m for m in messages))
        response = self.api.get('/api/candidates/', {'status': 'rejected', 'project_id': p.id})
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(self.api.get('/api/candidates/', {'project_id': 'bad'}).status_code, 400)
