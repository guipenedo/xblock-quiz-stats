function QuizStatsXBlock(runtime, element, context) {
    function xblock($, _) {
        let template = _.template($(element).find("#quiz_stats_tmpl_" + context.xblock_id).text());
        let answer_template = _.template($(element).find("#quiz_stats_tmpl_student_answer_" + context.xblock_id).text());

        function updateData(){
            const load_stats_url = runtime.handlerUrl(element, 'load_stats');
            $.get(load_stats_url, function (data) {
                render(data);
            });
        }


        function render(data) {
            // Render template
            data = processData(data);
            $(element).find('#quiz_stats_content_' + context.xblock_id).html(template(data));
            // charts
            if (data.nr_submissions > 0)
                Highcharts.chart('point_distribution', {
                title: {
                    text: 'Distribuição de pontos'
                },
                xAxis: {
                    title: {
                     text: 'Pontos obtidos'
                    }
                },
                yAxis: {
                    title: {
                        text: 'Número de alunos'
                    }
                },
                series: [{
                    name: 'Histogram',
                    type: 'histogram',
                    xAxis: 0,
                    yAxis: 0,
                    baseSeries: 1,
                    zIndex: -1,
                    pointPadding: 0.005,
                    showInLegend: false
                }, {
                    data: data.scores,
                    visible: false,
                    showInLegend: false
                }]
            });
            for (let pi = 0; pi < data.questions.length; pi++) {
                let q = data.questions[pi];
                let points = [];
                for (let option in q['options']){
                    let count = q['options'][option]['count'];
                    let correct = q['options'][option]['correct'];
                    points.push({percentage: (count/data.nr_submissions*100).toFixed(1),name: (correct ? '✓ ' : '') + option, y: count, color: correct ? 'green': 'grey'})
                }
                Highcharts.chart('question_chart_' + pi, {
                    title: {
                        text: q['title']
                    },
                    plotOptions: {
                        bar: {
                            dataLabels: {
                                enabled: true,
                                formatter: function () {
                                    return this.point.y + ' (' + this.point.percentage + '%)';
                                },
                                style: {
                                    fontSize: '15px'
                                }
                            }
                        }
                    },
                    subtitle: {
                        text: q['correct_count'] + '/' + data.nr_submissions + ' respostas corretas',
                    },
                    xAxis: {
                        type: 'category',
                        labels: {
                            style: {
                                fontSize: '15px'
                            }
                        }
                    },
                    yAxis: {
                        title: {
                            text: 'Número de respostas',
                            style: {
                                fontSize: '18px'
                            }
                        },
                        labels: {
                            enabled: false
                        }
                    },
                    series: [{
                        type: 'bar',
                        name: 'Contagem',
                        color: 'grey',
                        showInLegend: false,
                        styledMode: false,
                        data: points
                    }]
                });
            }
            // turmas
            if (data.is_course_cohorted) {
                let turmas_filter = $('#turmas_filter_' + context.xblock_id);
                if (data.cohort)
                    turmas_filter.val(data.cohort)
                turmas_filter.on('change', function () {
                    const change_cohort_handlerurl = runtime.handlerUrl(element, 'change_cohort');
                    context.cohort = this.value;
                    $.post(change_cohort_handlerurl, JSON.stringify({
                        'cohort': this.value
                    }), () => {
                        updateData()
                    });
                });
            }
            $(element).find('.view-student-answer')
                .leanModal()
                .on('click', function () {
                    let student_i = $(this).data('student-i');
                    $(element).find('#view_student_answer_' + context.xblock_id + ' .inner-wrapper').html(answer_template({
                        ...data.submissions[student_i],
                        max_score: data.max_score,
                        questions: data.questions
                    }));
                });
        }

        $(function () { // onLoad
            render(context.data);
        });
    }

    function processData(data){
        let scores = []; // list of scores to calculate average, median, etc
        let sum_scores = 0; // sum of scores for avg calculation
        let max_score = -1; // max score for this problem

        let student_scores = []; // dictionary with 'username', 'score', 'timestamp', 'answers'
        let no_submission = []; // list of users that have not submitted yet

        let question_titles = []; // list of question prompts/titles
        let question_options = []; // l[i] is a dictionary with answer: {count, correct}
        let question_correct_count = []; // l[i] is list of correct options for question i

        function init_answer(qi, answer) {
            if (!(answer in question_options[qi]))
                question_options[qi][answer] = {
                    'count': 0,
                    'correct': false
                }
        }

        for (let useri = 0; useri < data.length; useri++) {
            let user_data = data[useri];
            // skip quem não submeteu
            if (!('user_states' in user_data)) {
                no_submission.push(user_data['username'])
                continue
            }

            // global scores
            let user_score = user_data['state']['score']['raw_earned'];
            scores.push(user_score);
            sum_scores += user_score;
            student_scores.push({
                'name': user_data['name'] ? user_data['name'] : user_data['username'],
                'score': user_score,
                'timestamp': Date.parse(user_data['state']['last_submission_time'])
            })
            if (max_score === -1)
                max_score = user_data['state']['score']['raw_possible'];

            // per question stats
            let u_s = user_data['user_states'];
            let student_questions = {};
            for (let j = 0; j < u_s.length; j++)
                if ("Question" in u_s[j]) {
                    let question_title = u_s[j]["Question"], answer = u_s[j]["Answer"],
                        correct = u_s[j]["Correct Answer"], qid = u_s[j]["Answer ID"];
                    let qi = question_titles.indexOf(question_title);
                    if (qi === -1) {
                        // queremos adicionar à lista de questões
                        qi = question_titles.length;
                        question_titles.push(question_title);
                        question_options.push({});
                        question_correct_count.push(0);
                    }

                    // add this answer
                    init_answer(qi, answer);
                    question_options[qi][answer]['count']++;
                    // if we do not have a correct answer yet
                    if (correct) {
                        init_answer(qi, correct);
                        question_options[qi][correct]['correct'] = true;
                    }
                    let was_correct = user_data['state']['correct_map'][qid]['correctness'] === 'correct';
                    if (was_correct) {
                        question_options[qi][answer]['correct'] = true;
                        question_correct_count[qi]++;
                    }

                    student_questions[question_title] = {
                        'answer': answer,
                        'correct': was_correct
                    };
                }
            student_scores[student_scores.length - 1]['student_questions'] = student_questions;
        }
        let questions = []; // title, correct_count, options
        for (let qi = 0; qi < question_titles.length; qi++) {
            questions.push({
                'title': question_titles[qi],
                'correct_count': question_correct_count[qi],
                'options': question_options[qi],
            })
        }
        scores.sort(); // sort scores increasing order
        student_scores.sort(function (a, b) {
            return b.timestamp - a.timestamp;
        }) // sort submissions from most recent to oldest
        function get_quest_nr(q) {
            let nr = parseInt(q.split(".")[0], 10);
            return isNaN(nr) ? 0 : nr;
        }
        questions.sort(function (a, b) {
            return get_quest_nr(a['title']) - get_quest_nr(b['title']);
        }) // sort submissions by question number
        for (let qii = 0; qii < questions.length; qii++) {
            questions[qii]['index'] = qii;
        }
        let questions_erradas = [...questions];
        questions_erradas.sort(function (a, b) {
            if (a['correct_count'] === b['correct_count'])
                return get_quest_nr(a['title']) - get_quest_nr(b['title'])
            return a['correct_count'] - b['correct_count'];
        }) // sort submissions by correct answers
        let N = scores.length;
        let mais_erradas = [];
        for (let mei = 0; mei < questions_erradas.length; mei++) {
            if (questions_erradas[mei]['correct_count'] < 0.7 * N)
                mais_erradas.push(questions_erradas[mei])
        }
        let processed_data = {
            'nr_submissions': N,
            'no_submission': no_submission,
            'max_score': max_score,
            'submissions': student_scores,
            'mais_erradas': mais_erradas,
            'questions': questions,
            'cohorts': context.cohorts,
            'is_course_cohorted': context.is_course_cohorted,
            'cohort': context.cohort,
            'scores': scores,
            'average': "-",
            'median': "-",
            'limits': ["-", "-"]
        };
        if (N > 0) {
            processed_data['average'] = (sum_scores / N).toFixed(2);
            processed_data['median'] = ((N % 2) === 0 ? 0.5 * (scores[Math.max(N / 2 - 1, 0)] + scores[N / 2]) : scores[Math.floor(N / 2)]).toFixed(2);
            processed_data['limits'] = [scores[0], scores[N - 1]];
        }
        return processed_data;
    }

    function loadjs(url) {
        $('<script>')
            .attr('type', 'text/javascript')
            .attr('src', url)
            .appendTo(element);
    }

    if (require === undefined) {
        /**
         * The LMS does not use require.js (although it loads it...) and
         * does not already load jquery.fileupload.  (It looks like it uses
         * jquery.ajaxfileupload instead.  But our XBlock uses
         * jquery.fileupload.
         */
        loadjs('/static/js/vendor/jQuery-File-Upload/js/jquery.iframe-transport.js');
        loadjs('/static/js/vendor/jQuery-File-Upload/js/jquery.fileupload.js');
        xblock($, _);
    } else {
        /**
         * Studio, on the other hand, uses require.js and already knows about
         * jquery.fileupload.
         */
        require(['jquery', 'underscore', 'jquery.fileupload'], xblock);
    }
}
