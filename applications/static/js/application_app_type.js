/* Filter the Application's app_type dropdown by the selected agreement's
   contract type. Options are rebuilt (not hidden) for cross-browser
   reliability. Uses window.jQuery because form Media can load before
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
