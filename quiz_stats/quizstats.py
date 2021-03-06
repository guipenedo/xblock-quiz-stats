import json
from collections import defaultdict

import pkg_resources
import six
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from edx_user_state_client.interface import XBlockUserState
from opaque_keys import InvalidKeyError
from web_fragments.fragment import Fragment
from webob.response import Response
from xblock.core import XBlock
from xblock.fields import Scope
from xblock.fields import String
from xblockutils.resources import ResourceLoader
from xblockutils.studio_editable import StudioEditableXBlockMixin
from xmodule.contentstore.django import contentstore
from xmodule.modulestore.django import modulestore
from xmodule.util.sandboxing import get_python_lib_zip

from xblock.completable import XBlockCompletionMode

from opaque_keys.edx.keys import UsageKey
from common.djangoapps.student.models import user_by_anonymous_id, get_user_by_username_or_email, CourseEnrollment
from lms.djangoapps.course_blocks.api import get_course_blocks
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.instructor_analytics.basic import get_response_state
from lms.djangoapps.instructor_task.tasks_helper.grades import ProblemResponses
from openedx.core.djangoapps.course_groups.cohorts import get_cohort, is_course_cohorted, get_course_cohorts, get_random_cohort

loader = ResourceLoader(__name__)

ITEM_TYPE = "quiz_stats"


def dump(obj):
    s = ""
    for attr in dir(obj):
        s += f"<li>obj.{attr} = {getattr(obj, attr)}</li>\n"
    return s


def resource_string(path):
    """Handy helper for getting resources from our kit."""
    data = pkg_resources.resource_string(__name__, path)
    return data.decode("utf8")


class QuizStatsXBlock(XBlock, StudioEditableXBlockMixin):
    display_name = String(display_name="display_name",
                          default="Quiz Stats",
                          scope=Scope.settings,
                          help="Nome do componente na plataforma")

    cohort = String(display_name="cohort",
                    default="",
                    scope=Scope.preferences,
                    help="Turma selecionada para todos os editores")

    editable_fields = 'display_name'
    has_author_view = True
    completion_mode = XBlockCompletionMode.EXCLUDED

    # ----------- Views -----------
    def author_view(self, _context):
        return Fragment(
            "<p>Clica em preview ou live para veres o conteúdo deste bloco.</p>"
            "<p>Notas:</p>"
            "<ol> <li>1. Esta unidade (stats) deverá estar imediatamente a seguir à unidade com o quiz (que preferencialmente será só um bloco).</li><li>2. Esta unidade (das stats) deverá ter a opção <b>Hide from learners --></b> ativada.</li>"
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

        data = {
            'xblock_id': self._get_xblock_loc(),
            'is_course_cohorted': is_course_cohorted(self.course_id),
            'cohorts': self.get_cohorts(),
            'cohort': self.cohort
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
    def get_quiz_data(self):
        pr_class = ProblemResponses().__class__
        user_id = user_by_anonymous_id(self.xmodule_runtime.anonymous_student_id).id
        course_key = self.course_id

        valid_cohorts = self.get_cohorts()

        usage_key = self.get_quiz_unit()
        if not usage_key:
            raise InvalidKeyError

        user = get_user_model().objects.get(pk=user_id)

        student_data = []

        store = modulestore()

        with store.bulk_operations(course_key):
            try:
                course_blocks = get_course_blocks(user, usage_key)
            except:
                raise QuizNotFound
            usernames = set()
            for title, path, block_key in pr_class._build_problem_list(course_blocks, usage_key):
                # Chapter and sequential blocks are filtered out since they include state
                # which isn't useful for this report.
                if block_key.block_type != "problem":
                    continue

                block = store.get_item(block_key)
                generated_report_data = defaultdict(list)

                # Blocks can implement the generate_report_data method to provide their own
                # human-readable formatting for user state.
                try:
                    user_state_iterator = iter_all_for_block(block_key)
                    for username, state in self.generate_report_data(block, user_state_iterator):
                        generated_report_data[username].append(state)
                except NotImplementedError:
                    pass
                cohorted = is_course_cohorted(self.course_id)

                def in_cohort(user):
                    if cohorted:
                        cohort = get_cohort(user, course_key, assign=False, use_cached=True)
                        if not cohort or cohort.name not in valid_cohorts or (self.cohort and cohort.name != self.cohort):
                            # skip this one if not on the requested cohort or has no cohort (instructor)
                            return False
                    return True

                responses = []
                for response in list_problem_responses(course_key, block_key):
                    # A block that has a single state per user can contain multiple responses
                    # within the same state.
                    try:
                        user = get_user_by_username_or_email(response['username'])
                    except User.DoesNotExist:
                        continue
                    usernames.add(user.username)
                    if not in_cohort(user):
                        continue
                    response['name'] = self.format_name(user.profile.name)
                    user_states = generated_report_data.get(response['username'])
                    response['state'] = json.loads(response['state'])
                    response['state'].pop('input_state', None)
                    response['state'].pop('student_answers', None)

                    if user_states:
                        response['user_states'] = user_states
                    responses.append(response)
                enrollments = CourseEnrollment.objects.filter(course_id=self.course_id)
                for enr in enrollments:
                    if enr.user.username in usernames:
                        continue
                    if in_cohort(enr.user):  # add missing students
                        student_data.append(
                            {'username': enr.user.username, 'name': self.format_name(enr.user.profile.name)})
                        usernames.add(enr.user.username)
                student_data += responses
        return student_data

    @XBlock.handler
    def load_stats(self, request, suffix=''):
        require(self.is_staff)
        try:
            quizdata = self.get_quiz_data()
        except QuizNotFound:
            return Response(json.dumps("keyerror"))
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

    def get_cohorts(self):
        cohorts = [""] + [group.name for group in get_course_cohorts(course_id=self.course_id)]
        random_cohort = get_random_cohort(self.course_id)
        if random_cohort and random_cohort.name in cohorts:
            cohorts.remove(random_cohort.name)
        return cohorts

    def _get_xblock_loc(self):
        """Returns trailing number portion of self.location"""
        return str(self.location).split('@')[-1]

    def format_name(self, name):
        names = name.split()
        if len(names) > 0:
            name = names[0]
        if len(names) > 1:
            name += " " + names[-1]
        return name

    @property
    def is_staff(self):
        return getattr(self.xmodule_runtime, 'user_is_staff', False)

    def generate_report_data(self, block, user_state_iterator, limit_responses=None):

        from capa.capa_problem import LoncapaProblem, LoncapaSystem

        if block.category != 'problem':
            raise NotImplementedError()

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

                try:
                    question_text = lcp.find_question_label(answer_id)
                    answer_text = lcp.find_answer_text(answer_id, current_answer=orig_answers)
                    correct_answer_text = lcp.find_correct_answer_text(answer_id)
                except AssertionError:
                    continue

                count += 1
                report = {
                    "answer_id": answer_id,  # _("Answer ID")
                    "question": question_text,  # _("Question")
                    "answer": answer_text,  # _("Answer")
                }
                if correct_answer_text is not None:
                    report["correct_answer"] = correct_answer_text  # _("Correct Answer")
                yield user_state.username, report

    def get_quiz_unit(self):
        # sacamos a unidade imediatamente antes desta
        verts = self.get_parent().get_parent().children
        for i in range(1, len(verts)):
            if verts[i].block_id == self.parent.block_id:
                return verts[i - 1]


def require(assertion):
    """
    Raises PermissionDenied if assertion is not true.
    """
    if not assertion:
        raise PermissionDenied


def iter_all_for_block(block_key, scope=Scope.user_state):
    """
    Return an iterator over the data stored in the block (e.g. a problem block).

    You get no ordering guarantees.If you're using this method, you should be running in an
    async task.

    Arguments:
        block_key: an XBlock's locator (e.g. :class:`~BlockUsageLocator`)
        scope (Scope): must be `Scope.user_state`

    Returns:
        an iterator over all data. Each invocation returns the next :class:`~XBlockUserState`
            object, which includes the block's contents.
    """
    if scope != Scope.user_state:
        raise ValueError("Only Scope.user_state is supported")

    results = StudentModule.objects.order_by('id').filter(module_state_key=block_key)
    p = Paginator(results, settings.USER_STATE_BATCH_SIZE)

    for page_number in p.page_range:
        page = p.page(page_number)

        for sm in page.object_list:
            state = json.loads(sm.state)

            if state == {}:
                continue
            try:
                yield XBlockUserState(sm.student.username, sm.module_state_key, state, sm.modified, scope)
            except User.DoesNotExist:
                pass


def list_problem_responses(course_key, problem_location):
    """
    Return responses to a given problem as a dict.

    list_problem_responses(course_key, problem_location)

    would return [
        {'username': u'user1', 'state': u'...'},
        {'username': u'user2', 'state': u'...'},
        {'username': u'user3', 'state': u'...'},
    ]

    where `state` represents a student's response to the problem
    identified by `problem_location`.
    """
    if isinstance(problem_location, UsageKey):
        problem_key = problem_location
    else:
        problem_key = UsageKey.from_string(problem_location)
    # Are we dealing with an "old-style" problem location?
    run = problem_key.run
    if not run:
        problem_key = UsageKey.from_string(problem_location).map_into_course(course_key)
    if problem_key.course_key != course_key:
        return []

    smdat = StudentModule.objects.filter(
        course_id=course_key,
        module_state_key=problem_key
    )
    smdat = smdat.order_by('student')

    for response in smdat:
        try:
            yield {'username': response.student.username, 'state': get_response_state(response)}
        except User.DoesNotExist:
            continue


class QuizNotFound(Exception):
    """Base class for exceptions in this module."""
    pass
