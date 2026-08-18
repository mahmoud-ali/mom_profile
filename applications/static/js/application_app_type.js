/* Filter the Application's app_type dropdown by the selected agreement's
   contract type. Options are rebuilt (not hidden) for cross-browser
   reliability. When app_type is a searchable autocomplete (select2), the
   compatibility rule is enforced on save by the restricted queryset, so the
   option rebuilding is skipped and the pick is simply reset when the
   agreement changes. Uses window.jQuery because form Media can load before
   jquery.init.js. */
(function ($) {
    "use strict";
    $(function () {
        var $agreement = $("#id_agreement");
        var $appType = $("#id_app_type");
        if (!$agreement.length || !$appType.length) {
            return;
        }
        var agreements = JSON.parse($agreement.attr("data-agreement-contracts") || "{}");

        if ($appType.hasClass("admin-autocomplete")) {
            // Searchable app_type: selecting a different agreement invalidates
            // the current pick; reset it so the user searches again.
            $agreement.on("change", function () {
                $appType.val("").trigger("change");
            });
            return;
        }

        var appTypes = JSON.parse($appType.attr("data-apptype-contracts") || "{}");
        var allOptions = $appType.find("option").map(function () { return this; }).get();

        function applyFilter() {
            var contractType = agreements[$agreement.val()] || "";
            var selected = $appType.val();
            $appType.empty();
            allOptions.forEach(function (opt) {
                var allowed = appTypes[opt.value] || [];
                var show = !contractType || !allowed.length || allowed.indexOf(contractType) !== -1;
                if (show) {
                    $appType.append(opt);
                }
            });
            $appType.val(selected);
        }

        $agreement.on("change", applyFilter);
        applyFilter();
    });
})(window.jQuery || window.$ || (window.django && window.django.jQuery));
