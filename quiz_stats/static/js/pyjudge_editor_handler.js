function replaceNewLines(str) {
    return str.replace(/(?:\r\n|\r|\n)/g, '<br>');
}

// helper to visually format difference between outputs
function formatOutputDiff(expected_out, output) {
    if (output == null)
        return "null";
    let i = 0, j = 0;
    while (j < output.length) {
        while (i < expected_out.length && expected_out[i] === '\n')
            i++;
        while (j < output.length && output[j] === '\n')
            j++;
        if (i >= expected_out.length || expected_out[i++] !== output[j])
            return output.substr(0, j) + '<span style="color:red;font-weight: bold">' + (output[j] === " " ? "[whitespace]" : output[j]) + '</span>' + output.substr(j + 1, output.length - j - 1);
        j++;
    }
    let formatted_output = output;
    if (i < expected_out.length)
        formatted_output += '[<span style="color:red;font-weight: bold">' + expected_out.substr(i, expected_out.length - i) + '</span>]';
    return formatted_output
}

function handleEditorResponse(response, feedbackElement, cb) {
    if (response.result === 'success') {
        feedbackElement.html("<i aria-hidden=\"true\" class=\"fa fa-check\" style=\"color:green\"></i> " + response.message);
    } else {
        if (response.exit_code === 0 && !response.stderr)
            feedbackElement.html("<span aria-hidden=\"true\" class=\"fa fa-times\" style=\"color:darkred\"></span> <b><u>Output incorreta no caso de teste " + response.test_case + "</u></b><br/><b>Input:</b> " + response.input + "<br/><b>Output esperada:</b> " + replaceNewLines(response.expected_output) + "<br/><b>=============</b><br/><b>Output do teu programa:</b> (primeira diferença a vermelho)<br/>" + replaceNewLines(formatOutputDiff(response.expected_output, response.student_output)))
        else
            feedbackElement.html("<span aria-hidden=\"true\" class=\"fa fa-times\" style=\"color:darkred\"></span> <b><u>Erro no caso de teste " + response.test_case + "</u></b><br/><b>Input:</b> " + response.input + "<br/><b>Output esperada:</b> " + replaceNewLines(response.expected_output) + "<br/><b>=============</b><br/><b>Exit highcharts:</b> " + response.exit_code + "<br/><b>Erro do teu programa:</b> " + replaceNewLines(response.stderr))
    }
    if (cb)
        cb(response.result);
}

function getCodeEditor(element, readOnly=false){
    let editor = ace.edit(element);
    editor.setOptions({
        maxLines: 50,
        minLines: 10,
        autoScrollEditorIntoView: true,
        theme: "ace/theme/monokai",
        showPrintMargin: false,
        mode: "ace/mode/python",
        fontSize: "14pt",
        enableBasicAutocompletion: true,
        enableSnippets: true,
        enableLiveAutocompletion: true
    });
    if (!readOnly) {
        ace.require("ace/ext/language_tools");
        editor.setOptions({
            enableBasicAutocompletion: true,
            enableSnippets: true,
            enableLiveAutocompletion: true
        });
    } else
        editor.setOptions({
            readOnly: true
        });
    return editor;
}
