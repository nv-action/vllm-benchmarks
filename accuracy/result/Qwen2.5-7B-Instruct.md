<div>
    <strong>Software environment</strong>: vllm-ascendaccuarcy-test-dev.
</div>
<div>
    <strong>Hardware environment</strong>: Atlas 800T A2 Series.
</div>
<div>
    <strong>Datasets</strong>: ceval-valid,mmlu,gsm8k.
</div>
<div>
    <strong>Run Command</strong>: python run-accuracy.py 
        --model Qwen/Qwen2.5-7B-Instruct 
        --output Qwen2.5-7B-Instruct.md
</div>
<div>&nbsp;</div>

| Task                  | Version | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|--------:|-------:|-------:|----------|--------:|-------:|
| ceval-valid                           |      2 | none   | 5      | acc_norm | ↑ 0.8001 | ± 0.0105 |
| - ceval-valid_accountant              |      2 | none   | 5      | acc    | ↑ 0.8776 | ± 0.0473 |
| - ceval-valid_advanced_mathematics    |      2 | none   | 5      | acc    | ↑ 0.4211 | ± 0.1164 |
| - ceval-valid_art_studies             |      2 | none   | 5      | acc    | ↑ 0.7273 | ± 0.0787 |
| - ceval-valid_basic_medicine          |      2 | none   | 5      | acc    | ↑ 0.9474 | ± 0.0526 |
| - ceval-valid_business_administration |      2 | none   | 5      | acc    | ↑ 0.8485 | ± 0.0634 |
| - ceval-valid_chinese_language_and_literature |      2 | none   | 5      | acc    | ↑ 0.6087 | ± 0.1041 |
| - ceval-valid_civil_servant           |      2 | none   | 5      | acc    | ↑ 0.8298 | ± 0.0554 |
| - ceval-valid_clinical_medicine       |      2 | none   | 5      | acc    | ↑ 0.7727 | ± 0.0914 |
| - ceval-valid_college_chemistry       |      2 | none   | 5      | acc    | ↑ 0.6250 | ± 0.1009 |
| - ceval-valid_college_economics       |      2 | none   | 5      | acc    | ↑ 0.7455 | ± 0.0593 |
| - ceval-valid_college_physics         |      2 | none   | 5      | acc    | ↑ 0.7368 | ± 0.1038 |
| - ceval-valid_college_programming     |      2 | none   | 5      | acc    | ↑ 0.8649 | ± 0.0570 |
| - ceval-valid_computer_architecture   |      2 | none   | 5      | acc    | ↑ 0.7143 | ± 0.1010 |
| - ceval-valid_computer_network        |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_discrete_mathematics    |      2 | none   | 5      | acc    | ↑ 0.2500 | ± 0.1118 |
| - ceval-valid_education_science       |      2 | none   | 5      | acc    | ↑ 0.8621 | ± 0.0652 |
| - ceval-valid_electrical_engineer     |      2 | none   | 5      | acc    | ↑ 0.6757 | ± 0.0780 |
| - ceval-valid_environmental_impact_assessment_engineer |      2 | none   | 5      | acc    | ↑ 0.7419 | ± 0.0799 |
| - ceval-valid_fire_engineer           |      2 | none   | 5      | acc    | ↑ 0.7419 | ± 0.0799 |
| - ceval-valid_high_school_biology     |      2 | none   | 5      | acc    | ↑ 0.8947 | ± 0.0723 |
| - ceval-valid_high_school_chemistry   |      2 | none   | 5      | acc    | ↑ 0.7368 | ± 0.1038 |
| - ceval-valid_high_school_chinese     |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_high_school_geography   |      2 | none   | 5      | acc    | ↑ 0.8947 | ± 0.0723 |
| - ceval-valid_high_school_history     |      2 | none   | 5      | acc    | ↑ 0.9000 | ± 0.0688 |
| - ceval-valid_high_school_mathematics |      2 | none   | 5      | acc    | ↑ 0.5000 | ± 0.1213 |
| - ceval-valid_high_school_physics     |      2 | none   | 5      | acc    | ↑ 0.7368 | ± 0.1038 |
| - ceval-valid_high_school_politics    |      2 | none   | 5      | acc    | ↑ 0.8947 | ± 0.0723 |
| - ceval-valid_ideological_and_moral_cultivation |      2 | none   | 5      | acc    | ↑ 0.9474 | ± 0.0526 |
| - ceval-valid_law                     |      2 | none   | 5      | acc    | ↑ 0.6667 | ± 0.0983 |
| - ceval-valid_legal_professional      |      2 | none   | 5      | acc    | ↑ 0.7391 | ± 0.0936 |
| - ceval-valid_logic                   |      2 | none   | 5      | acc    | ↑ 0.6364 | ± 0.1050 |
| - ceval-valid_mao_zedong_thought      |      2 | none   | 5      | acc    | ↑ 0.9583 | ± 0.0417 |
| - ceval-valid_marxism                 |      2 | none   | 5      | acc    | ↑ 0.9474 | ± 0.0526 |
| - ceval-valid_metrology_engineer      |      2 | none   | 5      | acc    | ↑ 0.8333 | ± 0.0777 |
| - ceval-valid_middle_school_biology   |      2 | none   | 5      | acc    | ↑ 0.9524 | ± 0.0476 |
| - ceval-valid_middle_school_chemistry |      2 | none   | 5      | acc    | ↑ 0.9500 | ± 0.0500 |
| - ceval-valid_middle_school_geography |      2 | none   | 5      | acc    | ↑ 0.9167 | ± 0.0833 |
| - ceval-valid_middle_school_history   |      2 | none   | 5      | acc    | ↑ 0.9091 | ± 0.0627 |
| - ceval-valid_middle_school_mathematics |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_middle_school_physics   |      2 | none   | 5      | acc    | ↑ 0.9474 | ± 0.0526 |
| - ceval-valid_middle_school_politics  |      2 | none   | 5      | acc    | ↑ 1.0000 | ± 0.0000 |
| - ceval-valid_modern_chinese_history  |      2 | none   | 5      | acc    | ↑ 0.9130 | ± 0.0601 |
| - ceval-valid_operating_system        |      2 | none   | 5      | acc    | ↑ 0.8421 | ± 0.0859 |
| - ceval-valid_physician               |      2 | none   | 5      | acc    | ↑ 0.8367 | ± 0.0533 |
| - ceval-valid_plant_protection        |      2 | none   | 5      | acc    | ↑ 0.8636 | ± 0.0749 |
| - ceval-valid_probability_and_statistics |      2 | none   | 5      | acc    | ↑ 0.5556 | ± 0.1205 |
| - ceval-valid_professional_tour_guide |      2 | none   | 5      | acc    | ↑ 0.8966 | ± 0.0576 |
| - ceval-valid_sports_science          |      2 | none   | 5      | acc    | ↑ 0.9474 | ± 0.0526 |
| - ceval-valid_tax_accountant          |      2 | none   | 5      | acc    | ↑ 0.8571 | ± 0.0505 |
| - ceval-valid_teacher_qualification   |      2 | none   | 5      | acc    | ↑ 0.9091 | ± 0.0438 |
| - ceval-valid_urban_and_rural_planner |      2 | none   | 5      | acc    | ↑ 0.8043 | ± 0.0591 |
| - ceval-valid_veterinary_medicine     |      2 | none   | 5      | acc    | ↑ 0.8261 | ± 0.0808 |
| mmlu                                  |      2 | none   | 5      | acc    | ↑ 0.7354 | ± 0.0036 |
| - humanities                          |      2 | none   | 5      | acc    | ↑ 0.6823 | ± 0.0065 |
| - formal_logic                        |      1 | none   | 5      | acc    | ↑ 0.6032 | ± 0.0438 |
| - high_school_european_history        |      1 | none   | 5      | acc    | ↑ 0.8606 | ± 0.0270 |
| - high_school_us_history              |      1 | none   | 5      | acc    | ↑ 0.8971 | ± 0.0213 |
| - high_school_world_history           |      1 | none   | 5      | acc    | ↑ 0.8861 | ± 0.0207 |
| - international_law                   |      1 | none   | 5      | acc    | ↑ 0.8430 | ± 0.0332 |
| - jurisprudence                       |      1 | none   | 5      | acc    | ↑ 0.7870 | ± 0.0396 |
| - logical_fallacies                   |      1 | none   | 5      | acc    | ↑ 0.8160 | ± 0.0304 |
| - moral_disputes                      |      1 | none   | 5      | acc    | ↑ 0.7919 | ± 0.0219 |
| - moral_scenarios                     |      1 | none   | 5      | acc    | ↑ 0.5911 | ± 0.0164 |
| - philosophy                          |      1 | none   | 5      | acc    | ↑ 0.7749 | ± 0.0237 |
| - prehistory                          |      1 | none   | 5      | acc    | ↑ 0.8519 | ± 0.0198 |
| - professional_law                    |      1 | none   | 5      | acc    | ↑ 0.5287 | ± 0.0127 |
| - world_religions                     |      1 | none   | 5      | acc    | ↑ 0.8655 | ± 0.0262 |
| - other                               |      2 | none   | 5      | acc    | ↑ 0.7683 | ± 0.0073 |
| - business_ethics                     |      1 | none   | 5      | acc    | ↑ 0.8300 | ± 0.0378 |
| - clinical_knowledge                  |      1 | none   | 5      | acc    | ↑ 0.7925 | ± 0.0250 |
| - college_medicine                    |      1 | none   | 5      | acc    | ↑ 0.6821 | ± 0.0355 |
| - global_facts                        |      1 | none   | 5      | acc    | ↑ 0.4800 | ± 0.0502 |
| - human_aging                         |      1 | none   | 5      | acc    | ↑ 0.7399 | ± 0.0294 |
| - management                          |      1 | none   | 5      | acc    | ↑ 0.8641 | ± 0.0339 |
| - marketing                           |      1 | none   | 5      | acc    | ↑ 0.9316 | ± 0.0165 |
| - medical_genetics                    |      1 | none   | 5      | acc    | ↑ 0.7900 | ± 0.0409 |
| - miscellaneous                       |      1 | none   | 5      | acc    | ↑ 0.8531 | ± 0.0127 |
| - nutrition                           |      1 | none   | 5      | acc    | ↑ 0.7941 | ± 0.0232 |
| - professional_accounting             |      1 | none   | 5      | acc    | ↑ 0.5745 | ± 0.0295 |
| - professional_medicine               |      1 | none   | 5      | acc    | ↑ 0.7647 | ± 0.0258 |
| - virology                            |      1 | none   | 5      | acc    | ↑ 0.5783 | ± 0.0384 |
| - social sciences                     |      2 | none   | 5      | acc    | ↑ 0.8307 | ± 0.0067 |
| - econometrics                        |      1 | none   | 5      | acc    | ↑ 0.6053 | ± 0.0460 |
| - high_school_geography               |      1 | none   | 5      | acc    | ↑ 0.8838 | ± 0.0228 |
| - high_school_government_and_politics |      1 | none   | 5      | acc    | ↑ 0.9378 | ± 0.0174 |
| - high_school_macroeconomics          |      1 | none   | 5      | acc    | ↑ 0.8000 | ± 0.0203 |
| - high_school_microeconomics          |      1 | none   | 5      | acc    | ↑ 0.8866 | ± 0.0206 |
| - high_school_psychology              |      1 | none   | 5      | acc    | ↑ 0.8954 | ± 0.0131 |
| - human_sexuality                     |      1 | none   | 5      | acc    | ↑ 0.7939 | ± 0.0355 |
| - professional_psychology             |      1 | none   | 5      | acc    | ↑ 0.7892 | ± 0.0165 |
| - public_relations                    |      1 | none   | 5      | acc    | ↑ 0.7182 | ± 0.0431 |
| - security_studies                    |      1 | none   | 5      | acc    | ↑ 0.7837 | ± 0.0264 |
| - sociology                           |      1 | none   | 5      | acc    | ↑ 0.8756 | ± 0.0233 |
| - us_foreign_policy                   |      1 | none   | 5      | acc    | ↑ 0.8600 | ± 0.0349 |
| - stem                                |      2 | none   | 5      | acc    | ↑ 0.6895 | ± 0.0080 |
| - abstract_algebra                    |      1 | none   | 5      | acc    | ↑ 0.5500 | ± 0.0500 |
| - anatomy                             |      1 | none   | 5      | acc    | ↑ 0.7333 | ± 0.0382 |
| - astronomy                           |      1 | none   | 5      | acc    | ↑ 0.8684 | ± 0.0275 |
| - college_biology                     |      1 | none   | 5      | acc    | ↑ 0.8472 | ± 0.0301 |
| - college_chemistry                   |      1 | none   | 5      | acc    | ↑ 0.5200 | ± 0.0502 |
| - college_computer_science            |      1 | none   | 5      | acc    | ↑ 0.7000 | ± 0.0461 |
| - college_mathematics                 |      1 | none   | 5      | acc    | ↑ 0.5000 | ± 0.0503 |
| - college_physics                     |      1 | none   | 5      | acc    | ↑ 0.5098 | ± 0.0497 |
| - computer_security                   |      1 | none   | 5      | acc    | ↑ 0.7800 | ± 0.0416 |
| - conceptual_physics                  |      1 | none   | 5      | acc    | ↑ 0.7404 | ± 0.0287 |
| - electrical_engineering              |      1 | none   | 5      | acc    | ↑ 0.7172 | ± 0.0375 |
| - elementary_mathematics              |      1 | none   | 5      | acc    | ↑ 0.6587 | ± 0.0244 |
| - high_school_biology                 |      1 | none   | 5      | acc    | ↑ 0.8516 | ± 0.0202 |
| - high_school_chemistry               |      1 | none   | 5      | acc    | ↑ 0.6207 | ± 0.0341 |
| - high_school_computer_science        |      1 | none   | 5      | acc    | ↑ 0.9100 | ± 0.0288 |
| - high_school_mathematics             |      1 | none   | 5      | acc    | ↑ 0.5519 | ± 0.0303 |
| - high_school_physics                 |      1 | none   | 5      | acc    | ↑ 0.6026 | ± 0.0400 |
| - high_school_statistics              |      1 | none   | 5      | acc    | ↑ 0.7083 | ± 0.0310 |
| - machine_learning                    |      1 | none   | 5      | acc    | ↑ 0.5625 | ± 0.0471 |
| gsm8k                                 |      2 | flexible-extract | 5      | exact_match | ↑ 0.8180 | ± 0.0106 |