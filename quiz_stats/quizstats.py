import json
from collections import defaultdict, Set

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
from xmodule.util.sandboxing import get_python_lib_zip

from xmodule.contentstore.django import contentstore

from common.djangoapps.student.models import user_by_anonymous_id, get_user_by_username_or_email, CourseEnrollment
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
                            for username, state in self.generate_report_data(block, user_state_iterator, max_count):
                                generated_report_data[username].append(state)
                        except NotImplementedError:
                            pass
                    cohorted = is_course_cohorted(self.course_id)

                    def in_cohort(user):
                        if cohorted:
                            cohort = get_cohort(user, course_key, assign=False, use_cached=True)
                            if not cohort or (self.cohort and cohort.name != self.cohort):
                                # skip this one if not on the requested cohort or has no cohort (instructor)
                                return False
                        return True

                    responses = []
                    usernames = set()
                    for response in list_problem_responses(course_key, block_key, max_count):
                        # A block that has a single state per user can contain multiple responses
                        # within the same state.
                        try:
                            user = get_user_by_username_or_email(response['username'])
                        except User.DoesNotExist:
                            continue
                        usernames.add(user.username)
                        if not in_cohort(user):
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
                    enrollments = CourseEnrollment.objects.filter(course_id=self.course_id)
                    print("wtf mate")
                    print(enrollments)
                    print(usernames)
                    for enr in enrollments:
                        if enr.user.username in usernames:
                            print(enr.user.username, "is in usernames")
                            continue
                        if in_cohort(enr.user):
                            student_data += [{'username': enr.user.username}]
                            print(enr.user, "in cohort")
                        else:
                            print(enr.user, "not in cohort")
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

    def generate_report_data(self, block, user_state_iterator, limit_responses=None):

        from capa.capa_problem import LoncapaProblem, LoncapaSystem

        if block.category != 'problem':
            raise NotImplementedError()

        if limit_responses == 0:
            # Don't even start collecting answers
            return

        capa_system = LoncapaSystem(
            ajax_url=None,
            # TODO set anonymous_student_id to the anonymous ID of the user which answered each problem
            # Anonymous ID is required for Matlab, CodeResponse, and some custom problems that include
            # '$anonymous_student_id' in their XML.
            # For the purposes of this report, we don't need to support those use cases.
            anonymous_student_id=None,
            cache=None,
            can_execute_unsafe_code=lambda: None,
            get_python_lib_zip=(lambda: get_python_lib_zip(contentstore, block.runtime.course_id)),
            DEBUG=None,
            filestore=block.runtime.resources_fs,
            i18n=block.runtime.service(block, "i18n"),
            node_path=None,
            render_template=None,
            seed=1,
            STATIC_URL=None,
            xqueue=None,
            matlab_api_key=None,
        )
        _ = capa_system.i18n.ugettext

        count = 0
        for user_state in user_state_iterator:
            if 'student_answers' not in user_state.state:
                continue

            lcp = LoncapaProblem(
                problem_text=block.data,
                id=block.location.html_id(),
                capa_system=capa_system,
                # We choose to run without a fully initialized CapaModule
                capa_module=None,
                state={
                    'done': user_state.state.get('done'),
                    'correct_map': user_state.state.get('correct_map'),
                    'student_answers': user_state.state.get('student_answers'),
                    'has_saved_answers': user_state.state.get('has_saved_answers'),
                    'input_state': user_state.state.get('input_state'),
                    'seed': user_state.state.get('seed'),
                },
                seed=user_state.state.get('seed'),
                # extract_tree=False allows us to work without a fully initialized CapaModule
                # We'll still be able to find particular data in the XML when we need it
                extract_tree=False,
            )

            for answer_id, orig_answers in lcp.student_answers.items():
                # Some types of problems have data in lcp.student_answers that isn't in lcp.problem_data.
                # E.g. formulae do this to store the MathML version of the answer.
                # We exclude these rows from the report because we only need the text-only answer.
                if answer_id.endswith('_dynamath'):
                    continue

                if limit_responses and count >= limit_responses:
                    # End the iterator here
                    return

                question_text = lcp.find_question_label(answer_id)
                try:
                    answer_text = lcp.find_answer_text(answer_id, current_answer=orig_answers)
                except AssertionError:
                    continue
                correct_answer_text = lcp.find_correct_answer_text(answer_id)

                count += 1
                report = {
                    "answer_id": answer_id, # _("Answer ID")
                    "question": question_text, # _("Question")
                    "answer": answer_text, # _("Answer")
                }
                if correct_answer_text is not None:
                    report["correct_answer"] = correct_answer_text # _("Correct Answer")
                yield user_state.username, report


def require(assertion):
    """
    Raises PermissionDenied if assertion is not true.
    """
    if not assertion:
        raise PermissionDenied
