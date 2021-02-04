import json
from collections import defaultdict

import pkg_resources
import six
from django.conf import settings
from django.contrib.auth import get_user_model
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import UsageKey
from web_fragments.fragment import Fragment
from xblock.core import XBlock
from xblock.fields import Scope, String
from xblockutils.resources import ResourceLoader
from xblockutils.studio_editable import StudioEditableXBlockMixin
from django.contrib.auth.models import User
from webob.response import Response
from django.core.exceptions import PermissionDenied
from xmodule.modulestore.django import modulestore
from openedx.core.djangoapps.course_groups.cohorts import get_cohort, is_course_cohorted, get_course_cohorts

from common.djangoapps.student.models import user_by_anonymous_id, get_user_by_username_or_email
from lms.djangoapps.course_blocks.api import get_course_blocks
from lms.djangoapps.courseware.user_state_client import DjangoXBlockUserStateClient
from lms.djangoapps.instructor_analytics.basic import list_problem_responses
from lms.djangoapps.instructor_task.tasks_helper.grades import ProblemResponses

loader = ResourceLoader(__name__)

ITEM_TYPE = "quiz_stats"


def resource_string(path):
    """Handy helper for getting resources from our kit."""
    data = pkg_resources.resource_string(__name__, path)
    return data.decode("utf8")


def dump(obj):
    s = ""
    for attr in dir(obj):
        s += f"<li>obj.{attr} = {getattr(obj, attr)}</li>\n"
    return s


class QuizStatsXBlock(XBlock, StudioEditableXBlockMixin):
    display_name = String(display_name="display_name",
                          default="Quiz Stats",
                          scope=Scope.settings,
                          help="Nome do componente na plataforma")

    block_location = String(display_name="block_location",
                            default="",
                            scope=Scope.settings,
                            help="A localização do bloco para o qual queremos mostrar stats. Se vários, separar com vírgula.")

    cohort = String(display_name="cohort",
                    default="",
                    scope=Scope.preferences,
                    help="Turma selecionada para todos os editores")

    editable_fields = ('display_name', 'block_location')
    has_author_view = True

    # ----------- Views -----------
    def author_view(self, _context):
        return Fragment(
            "<p>Clica em preview ou live para veres o conteúdo deste bloco.</p>"
            "<p>Notas:</p>"
            "<ol> <li>1. Clicar em edit e colocar a localização do bloco quiz (informação de depuração para a equipa > location). Se vários, separar com vírgula.</li>"
            "<li>2. Esta unidade (das stats) deverá ter a opção <b>Hide from learners --></b> ativada.</li>"
            "<li>3. As questões do quiz deverão estar numeradas (começar com 1., 2., etc) para garantir a correta ordenação das stats.</li></ol>")

    # noinspection PyProtectedMember
    def student_view(self, _context):
        """
            The view students see
                :param _context:
                :return:
        """
        if not self.is_staff:
            return Fragment("Erro: esta página só está disponível para instrutores")
        try:
            quizdata = self.get_quiz_data()
        except InvalidKeyError:
            return Fragment("Erro ao procurar o quiz - corrige a localização nas settings!")
        data = {
            'xblock_id': self._get_xblock_loc(),
            'is_course_cohorted': is_course_cohorted(self.course_id),
            'cohorts': [group.name for group in get_course_cohorts(course_id=self.course_id)],
            'cohort': self.cohort,
            'data': quizdata
        }
        html = loader.render_django_template('templates/stats_display.html', data)
        frag = Fragment(html)

        frag.add_css(resource_string("static/css/quiz_stats.css"))

        frag.add_css(resource_string("static/highcharts/css/highcharts.css"))
        frag.add_javascript(resource_string("static/highcharts/highcharts.js"))
        frag.add_javascript(resource_string("static/highcharts/modules/histogram-bellcurve.js"))

        frag.add_javascript(resource_string("static/js/stats_script.js"))
        frag.initialize_js('QuizStatsXBlock', data)

        return frag

    # ----------- Handlers -----------
    def find_previous_unit_location(self):
        children = self.get_parent().get_children()
        for bi in range(len(children) - 1):
            if children[bi + 1].block_location == self.block_location:
                return children[bi].block_location
        return None

    def get_quiz_data(self):
        pr_class = ProblemResponses().__class__
        user_id = user_by_anonymous_id(self.xmodule_runtime.anonymous_student_id).id
        course_key = self.course_id
        usage_keys = [
            UsageKey.from_string(usage_key_str).map_into_course(course_key)
            for usage_key_str in self.block_location.split(",")
        ]
        user = get_user_model().objects.get(pk=user_id)

        student_data = []
        max_count = settings.FEATURES.get('MAX_PROBLEM_RESPONSES_COUNT')

        store = modulestore()
        user_state_client = DjangoXBlockUserStateClient()

        with store.bulk_operations(course_key):
            for usage_key in usage_keys:
                course_blocks = get_course_blocks(user, usage_key)
                for title, path, block_key in pr_class._build_problem_list(course_blocks, usage_key):
                    # Chapter and sequential blocks are filtered out since they include state
                    # which isn't useful for this report.
                    if block_key.block_type in ('sequential', 'chapter'):
                        continue

                    block = store.get_item(block_key)
                    generated_report_data = defaultdict(list)

                    # Blocks can implement the generate_report_data method to provide their own
                    # human-readable formatting for user state.
                    if hasattr(block, 'generate_report_data'):
                        try:
                            user_state_iterator = user_state_client.iter_all_for_block(block_key)
                            for username, state in block.generate_report_data(user_state_iterator, max_count):
                                generated_report_data[username].append(state)
                        except NotImplementedError:
                            pass

                    responses = []

                    for response in list_problem_responses(course_key, block_key, max_count):
                        # A block that has a single state per user can contain multiple responses
                        # within the same state.
                        try:
                            user = get_user_by_username_or_email(response['username'])
                        except User.DoesNotExist:
                            continue
                        cohort = get_cohort(user, course_key, assign=False, use_cached=True)
                        if self.cohort and (not cohort or cohort.name != self.cohort):
                            # skip this one if not on the requested cohort
                            continue
                        names = user.profile.name.split()
                        if len(names) > 0:
                            response['name'] = names[0]
                        if len(names) > 1:
                            response['name'] += " " + names[-1]
                        user_states = generated_report_data.get(response['username'])
                        response['state'] = json.loads(response['state'])
                        response['state'].pop('input_state', None)
                        response['state'].pop('student_answers', None)

                        if user_states:
                            response['user_states'] = user_states
                        responses.append(response)

                    student_data += responses
        return student_data

    @XBlock.handler
    def load_stats(self, request, suffix=''):
        require(self.is_staff)
        try:
            quizdata = self.get_quiz_data()
        except InvalidKeyError:
            return Fragment("Erro ao procurar o quiz - corrige a localização nas settings!")
        return Response(json.dumps(quizdata))

    @XBlock.json_handler
    def change_cohort(self, data, _suffix):
        self.cohort = data["cohort"]
        return {
            'result': 'success'
        }

    def block_id(self):
        """
        Return the usage_id of the block.
        """
        return six.text_type(self.scope_ids.usage_id)

    def block_course_id(self):
        """
        Return the course_id of the block.
        """
        return six.text_type(self.course_id)

    def _get_xblock_loc(self):
        """Returns trailing number portion of self.location"""
        return str(self.location).split('@')[-1]

    @property
    def is_staff(self):
        return getattr(self.xmodule_runtime, 'user_is_staff', False)


def require(assertion):
    """
    Raises PermissionDenied if assertion is not true.
    """
    if not assertion:
        raise PermissionDenied
