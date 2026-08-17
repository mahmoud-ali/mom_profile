/* Dependent dropdown: «نوع الحدث التاريخي» options follow the selected
   «تصنيف الحدث التاريخي» (data-event-labels on #id_event_label).
   Options are rebuilt (not hidden) for cross-browser reliability. Uses
   window.jQuery because form Media can load before jquery.init.js. */
(function ($) {
    "use strict";
    $(function () {
        var $category = $("#id_event_category");
        var $label = $("#id_event_label");
        if (!$category.length || !$label.length) {
            return;
        }
        var labels = JSON.parse($label.attr("data-event-labels") || "{}");
        var allOptions = $label.find("option").map(function () { return this; }).get();

        function applyFilter() {
            var category = $category.val() || "";
            var allowed = {};
            (labels[category] || []).forEach(function (pair) {
                allowed[pair[0]] = true;
            });
            var selected = $label.val();
            $label.empty();
            allOptions.forEach(function (opt) {
                var show = !opt.value || !category || allowed[opt.value];
                if (show) {
                    $label.append(opt);
                }
            });
            $label.val(selected);
        }

        $category.on("change", applyFilter);
        applyFilter();
    });
})(window.jQuery || window.$ || (window.django && window.django.jQuery));
