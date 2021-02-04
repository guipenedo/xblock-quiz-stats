function PythonJudgeXBlock(runtime, element, context) {
    let id = context.xblock_id;

    ace.require("ace/ext/language_tools");
    let editor = getCodeEditor("student_code_" + id)

    // helper to disable and enable buttons
    function switchButtons(disabled){
        $(element).find('#submit_' + id).prop("disabled", disabled);
        $(element).find('#run_' + id).prop("disabled", disabled);
        $(element).find('#highcharts-runner-button_' + id).prop("disabled", disabled);
    }

    // submit
    $(element).find('#submit_' + id).bind('click', function() {
        $(element).find('#highcharts-runner_' + id).hide();
        $(element).find('#run_' + id).show();
        const data = {
            'student_code': editor.getValue()
        };
        switchButtons(true);
        const handlerUrl = runtime.handlerUrl(element, 'submit_code');
        $.post(handlerUrl, JSON.stringify(data)).done(function (response) {
            switchButtons(false);
            handleEditorResponse(response, $("#highcharts-feedback_" + id), (result) => {
                if (result === 'success')
                    $("#model_answer_container_" + id).show();
            })
        });
    });

    // run button
    $(element).find('#run_' + id).bind('click', function () {
        $(this).hide();
        $(element).find('#highcharts-runner_' + id).show();
    });

    // run button inside the runner window
    $(element).find('#highcharts-runner-button_' + id).bind('click', function () {
        const data = {
            'student_code': editor.getValue(),
            'input': $('#highcharts-runner-input_' + id).val()
        };
        switchButtons(true);
        const handlerUrl = runtime.handlerUrl(element, 'run_code');
        $.post(handlerUrl, JSON.stringify(data)).done(function (response) {
            switchButtons(false);
            if (response.exit_code === 0 && !response.stderr)
                $("#highcharts-runner-output_" + id).html(replaceNewLines(response.stdout));
            else
                $("#highcharts-runner-output_" + id).html("<u>Erro de execução.</u> Exit highcharts: <b>" + response.exit_code + "</b><br /><b>Output:</b> " + replaceNewLines(response.stdout ? response.stdout : "?") + "<br /><b>Stderr:</b> " + replaceNewLines(response.stderr));

        }).fail(function () {
            switchButtons(false);
            $("#highcharts-runner-output_" + id).text("Erro desconhecido. Por favor tenta novamente mais tarde.");
        });
    });

    // view answer button
    let view_model_answer_editor = getCodeEditor("view_model_answer_" + id, true)
    $(element).find('#model_answer_button_' + id)
        .leanModal()
        .on('click', function () {
            const handlerUrl = runtime.handlerUrl(element, 'get_model_answer');
            $.post(handlerUrl, "{}").done(function (response) {
                if (response.result === 'success')
                    view_model_answer_editor.setValue(response.model_answer);
                else
                    view_model_answer_editor.setValue(response.message);
            });
        });

    if(context.is_course_staff) {
        $("#model_answer_container_" + id).show();

        let view_submission_editor = getCodeEditor("view_student_code_" + id, true)

        $(element).find('.view_code_button_' + id)
            .leanModal()
            .on('click', function () {
                let row = $(this).parents("tr");
                $(element).find('#view_code_student_name_' + id).text(row.data('fullname'));
                view_submission_editor.setValue(row.data('student_code'));
                handleEditorResponse(row.data('evaluation'), $("#view_code_feedback_" + id));
            });

        $.tablesorter.addParser({
            id: "data_pt",
            is: function (_s) {
                return false;
            },
            format: function (s, _table, _cell, _cellIndex) {
                const mesHash = {
                    'Janeiro': 1, 'Fevereiro': 2, 'Março': 3, 'Abril': 4, 'Maio': 5, 'Junho': 6, 'Julho': 7, 'Agosto': 8, 'Setembro': 9, 'Outubro': 10, 'Novembro': 11, 'Dezembro': 12
                };
                let matches = s.match(/(de)\s+(\w*)\s+(de)/);
                if (!matches)
                    return 0;
                let mes = matches[2];
                s = s.replace(mes, mesHash[mes])
                    // replace separators
                    .replace(/\s+(de)\s+/g, "/").replace(/\s+(às)\s+/g, " ")
                    // reformat dd/mm/yy to mm/dd/yy
                    .replace(/(\d{1,2})[\/\s](\d{1,2})[\/\s](\d{2})/, "$2/$1/$3");
                return (new Date(s)).getTime();
            },
            type: "numeric"
        });

        let table_options = {
            theme: 'blue',
            headers: {
                2: {
                  sorter: "data_pt"
                }
            }
        };
        if (context.is_course_cohorted) {
            let turmas_filter = $('#turmas_filter_' + id);
            table_options = {
                widgets: ['zebra', 'filter'],
                widgetOptions: {
                    filter_columnFilters: false,
                    filter_external: turmas_filter
                },
                ...table_options
            }
            turmas_filter.on('change', function () {
                const change_cohort_handlerurl = runtime.handlerUrl(element, 'change_cohort');
                $.post(change_cohort_handlerurl, JSON.stringify({
                    'cohort': this.value
                }));
            });
        }
        $("#submissions_" + id).tablesorter(table_options);
    }


    //autosave every 10 seconds
    let previous_code = editor.getValue();
    const autosave_handlerurl = runtime.handlerUrl(element, 'autosave_code');
    setInterval(() => {
        if (previous_code !== editor.getValue()){
            previous_code = editor.getValue();
            const data = {
                'student_code': previous_code
            };
            $.post(autosave_handlerurl, JSON.stringify(data));
        }
    }, 10*1000);

    if (context.last_output)
        handleEditorResponse(context.last_output, $("#highcharts-feedback_" + id));

    if(context.no_submission && context.no_submission === true)
        $(element).find('#highcharts-runner_' + id).show();
}
