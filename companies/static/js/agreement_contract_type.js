/* Dependent dropdowns for the Agreement forms (options rebuilt, not hidden,
   for cross-browser reliability):
   - contract_type options by the selected company's type
     (data-contract-map on #id_contract_type, data-companies on #id_company).
   - locality options by the selected state
     (data-locality-map on #id_locality, listening to #id_state).
   Company page: agreement inline rows (agreements-<n>-...) filter contract_type
   by the page's company_type (#id_company_type) and locality by the row's state.
   NOTE: form Media can load before jquery.init.js, so use window.jQuery. */
(function ($) {
    "use strict";

    function filterSelect($select, allowed) {
        var all = $select.data("all-options");
        if (!all) {
            all = $select.find("option").map(function () { return this; }).get();
            $select.data("all-options", all);
        }
        var selected = $select.val();
        $select.empty();
        all.forEach(function (opt) {
            var show = !opt.value || !allowed.length || allowed.indexOf(opt.value) !== -1;
            if (show) {
                $select.append(opt);
            }
        });
        $select.val(selected);
    }

    $(function () {
        // --- standalone Agreement form ---
        var $contract = $("#id_contract_type");
        if ($contract.length && $("#id_company").length) {
            var map = JSON.parse($contract.attr("data-contract-map") || "{}");
            var companies = JSON.parse($("#id_company").attr("data-companies") || "{}");
            function applyContractFilter() {
                var companyType = companies[$("#id_company").val()] || "";
                filterSelect($contract, map[companyType] || []);
            }
            $("#id_company").on("change", applyContractFilter);
            applyContractFilter();
        }

        var $locality = $("#id_locality");
        if ($locality.length && $("#id_state").length) {
            var localityMap = JSON.parse($locality.attr("data-locality-map") || "{}");
            var allLocality = $locality.find("option").map(function () { return this; }).get();
            function applyLocalityFilter() {
                var stateId = String($("#id_state").val() || "");
                var selected = $locality.val();
                $locality.empty();
                allLocality.forEach(function (opt) {
                    var show = !stateId || String(localityMap[opt.value] || "") === stateId;
                    if (show) {
                        $locality.append(opt);
                    }
                });
                $locality.val(selected);
            }
            $("#id_state").on("change", applyLocalityFilter);
            applyLocalityFilter();
        }

        // --- Company page: agreement inline rows ---
        var $companyType = $("#id_company_type");
        if ($companyType.length) {
            function applyInlineContractFilter() {
                var companyType = $companyType.val() || "";
                $('select[name$="-contract_type"]').each(function () {
                    var rowMap = JSON.parse($(this).attr("data-contract-map") || "{}");
                    filterSelect($(this), rowMap[companyType] || []);
                });
            }
            $companyType.on("change", applyInlineContractFilter);
            applyInlineContractFilter();
        }

        $(document).on("change", 'select[name$="-state"]', function () {
            var prefix = this.name.replace(/-state$/, "");
            var $loc = $('select[name="' + prefix + '-locality"]');
            if (!$loc.length) {
                return;
            }
            var rowMap = JSON.parse($loc.attr("data-locality-map") || "{}");
            var stateId = String($(this).val() || "");
            var selected = $loc.val();
            $loc.empty();
            var all = $loc.find("option").map(function () { return this; }).get();
            all.forEach(function (opt) {
                var show = !stateId || String(rowMap[opt.value] || "") === stateId;
                if (show) {
                    $loc.append(opt);
                }
            });
            $loc.val(selected);
        });
    });
})(window.jQuery || window.$ || (window.django && window.django.jQuery));
