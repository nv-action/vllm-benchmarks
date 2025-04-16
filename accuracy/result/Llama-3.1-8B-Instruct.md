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
        --model meta-llama/Llama-3.1-8B-Instruct 
        --output Llama-3.1-8B-Instruct.md
</div>
<div>&nbsp;</div>

| Task                  | Version | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|--------:|-------:|-------:|----------|--------:|-------:|
| ceval-valid                           |      2 | none   | 5      | acc    | ↑ 0.5483 | ± 0.0132 |
| - ceval-valid_accountant              |      2 | none   | 5      | acc    | ↑ 0.4898 | ± 0.0722 |
| - ceval-valid_advanced_mathematics    |      2 | none   | 5      | acc    | ↑ 0.5263 | ± 0.1177 |
| - ceval-valid_art_studies             |      2 | none   | 5      | acc    | ↑ 0.5455 | ± 0.0880 |
| - ceval-valid_basic_medicine          |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_business_administration |      2 | none   | 5      | acc    | ↑ 0.6061 | ± 0.0864 |
| - ceval-valid_chinese_language_and_literature |      2 | none   | 5      | acc    | ↑ 0.4348 | ± 0.1057 |
| - ceval-valid_civil_servant           |      2 | none   | 5      | acc    | ↑ 0.4894 | ± 0.0737 |
| - ceval-valid_clinical_medicine       |      2 | none   | 5      | acc    | ↑ 0.5455 | ± 0.1087 |
| - ceval-valid_college_chemistry       |      2 | none   | 5      | acc    | ↑ 0.4167 | ± 0.1028 |
| - ceval-valid_college_economics       |      2 | none   | 5      | acc    | ↑ 0.4545 | ± 0.0678 |
| - ceval-valid_college_physics         |      2 | none   | 5      | acc    | ↑ 0.4737 | ± 0.1177 |
| - ceval-valid_college_programming     |      2 | none   | 5      | acc    | ↑ 0.5946 | ± 0.0818 |
| - ceval-valid_computer_architecture   |      2 | none   | 5      | acc    | ↑ 0.5714 | ± 0.1107 |
| - ceval-valid_computer_network        |      2 | none   | 5      | acc    | ↑ 0.7895 | ± 0.0961 |
| - ceval-valid_discrete_mathematics    |      2 | none   | 5      | acc    | ↑ 0.4375 | ± 0.1281 |
| - ceval-valid_education_science       |      2 | none   | 5      | acc    | ↑ 0.7241 | ± 0.0845 |
| - ceval-valid_electrical_engineer     |      2 | none   | 5      | acc    | ↑ 0.4324 | ± 0.0826 |
| - ceval-valid_environmental_impact_assessment_engineer |      2 | none   | 5      | acc    | ↑ 0.5484 | ± 0.0909 |
| - ceval-valid_fire_engineer           |      2 | none   | 5      | acc    | ↑ 0.4839 | ± 0.0912 |
| - ceval-valid_high_school_biology     |      2 | none   | 5      | acc    | ↑ 0.5263 | ± 0.1177 |
| - ceval-valid_high_school_chemistry   |      2 | none   | 5      | acc    | ↑ 0.4737 | ± 0.1177 |
| - ceval-valid_high_school_chinese     |      2 | none   | 5      | acc    | ↑ 0.2105 | ± 0.0961 |
| - ceval-valid_high_school_geography   |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_high_school_history     |      2 | none   | 5      | acc    | ↑ 0.6500 | ± 0.1094 |
| - ceval-valid_high_school_mathematics |      2 | none   | 5      | acc    | ↑ 0.0000 | ± 0.0000 |
| - ceval-valid_high_school_physics     |      2 | none   | 5      | acc    | ↑ 0.3158 | ± 0.1096 |
| - ceval-valid_high_school_politics    |      2 | none   | 5      | acc    | ↑ 0.5789 | ± 0.1164 |
| - ceval-valid_ideological_and_moral_cultivation |      2 | none   | 5      | acc    | ↑ 0.8947 | ± 0.0723 |
| - ceval-valid_law                     |      2 | none   | 5      | acc    | ↑ 0.4583 | ± 0.1039 |
| - ceval-valid_legal_professional      |      2 | none   | 5      | acc    | ↑ 0.3913 | ± 0.1041 |
| - ceval-valid_logic                   |      2 | none   | 5      | acc    | ↑ 0.5000 | ± 0.1091 |
| - ceval-valid_mao_zedong_thought      |      2 | none   | 5      | acc    | ↑ 0.5000 | ± 0.1043 |
| - ceval-valid_marxism                 |      2 | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
| - ceval-valid_metrology_engineer      |      2 | none   | 5      | acc    | ↑ 0.5833 | ± 0.1028 |
| - ceval-valid_middle_school_biology   |      2 | none   | 5      | acc    | ↑ 0.7143 | ± 0.1010 |
| - ceval-valid_middle_school_chemistry |      2 | none   | 5      | acc    | ↑ 0.8500 | ± 0.0819 |
| - ceval-valid_middle_school_geography |      2 | none   | 5      | acc    | ↑ 0.5833 | ± 0.1486 |
| - ceval-valid_middle_school_history   |      2 | none   | 5      | acc    | ↑ 0.5455 | ± 0.1087 |
| - ceval-valid_middle_school_mathematics |      2 | none   | 5      | acc    | ↑ 0.3684 | ± 0.1137 |
| - ceval-valid_middle_school_physics   |      2 | none   | 5      | acc    | ↑ 0.6316 | ± 0.1137 |
| - ceval-valid_middle_school_politics  |      2 | none   | 5      | acc    | ↑ 0.8095 | ± 0.0878 |
| - ceval-valid_modern_chinese_history  |      2 | none   | 5      | acc    | ↑ 0.5217 | ± 0.1065 |
| - ceval-valid_operating_system        |      2 | none   | 5      | acc    | ↑ 0.6316 | ± 0.1137 |
| - ceval-valid_physician               |      2 | none   | 5      | acc    | ↑ 0.5918 | ± 0.0709 |
| - ceval-valid_plant_protection        |      2 | none   | 5      | acc    | ↑ 0.7727 | ± 0.0914 |
| - ceval-valid_probability_and_statistics |      2 | none   | 5      | acc    | ↑ 0.3889 | ± 0.1182 |
| - ceval-valid_professional_tour_guide |      2 | none   | 5      | acc    | ↑ 0.6207 | ± 0.0917 |
| - ceval-valid_sports_science          |      2 | none   | 5      | acc    | ↑ 0.6316 | ± 0.1137 |
| - ceval-valid_tax_accountant          |      2 | none   | 5      | acc    | ↑ 0.3878 | ± 0.0703 |
| - ceval-valid_teacher_qualification   |      2 | none   | 5      | acc    | ↑ 0.7955 | ± 0.0615 |
| - ceval-valid_urban_and_rural_planner |      2 | none   | 5      | acc    | ↑ 0.5217 | ± 0.0745 |
| - ceval-valid_veterinary_medicine     |      2 | none   | 5      | acc    | ↑ 0.6087 | ± 0.1041 |
| mmlu                                  |      2 | none   | 5      | acc    | ↑ 0.6867 | ± 0.0037 |
| - humanities                          |      2 | none   | 5      | acc    | ↑ 0.6495 | ± 0.0067 |
| - formal_logic                        |      1 | none   | 5      | acc    | ↑ 0.5714 | ± 0.0443 |
| - high_school_european_history        |      1 | none   | 5      | acc    | ↑ 0.7636 | ± 0.0332 |
| - high_school_us_history              |      1 | none   | 5      | acc    | ↑ 0.8186 | ± 0.0270 |
| - high_school_world_history           |      1 | none   | 5      | acc    | ↑ 0.8439 | ± 0.0236 |
| - international_law                   |      1 | none   | 5      | acc    | ↑ 0.8347 | ± 0.0339 |
| - jurisprudence                       |      1 | none   | 5      | acc    | ↑ 0.7778 | ± 0.0402 |
| - logical_fallacies                   |      1 | none   | 5      | acc    | ↑ 0.8098 | ± 0.0308 |
| - moral_disputes                      |      1 | none   | 5      | acc    | ↑ 0.7630 | ± 0.0229 |
| - moral_scenarios                     |      1 | none   | 5      | acc    | ↑ 0.5687 | ± 0.0166 |
| - philosophy                          |      1 | none   | 5      | acc    | ↑ 0.7363 | ± 0.0250 |
| - prehistory                          |      1 | none   | 5      | acc    | ↑ 0.7562 | ± 0.0239 |
| - professional_law                    |      1 | none   | 5      | acc    | ↑ 0.5111 | ± 0.0128 |
| - world_religions                     |      1 | none   | 5      | acc    | ↑ 0.8363 | ± 0.0284 |
| - other                               |      2 | none   | 5      | acc    | ↑ 0.7448 | ± 0.0075 |
| - business_ethics                     |      1 | none   | 5      | acc    | ↑ 0.7200 | ± 0.0451 |
| - clinical_knowledge                  |      1 | none   | 5      | acc    | ↑ 0.7509 | ± 0.0266 |
| - college_medicine                    |      1 | none   | 5      | acc    | ↑ 0.6821 | ± 0.0355 |
| - global_facts                        |      1 | none   | 5      | acc    | ↑ 0.3900 | ± 0.0490 |
| - human_aging                         |      1 | none   | 5      | acc    | ↑ 0.6951 | ± 0.0309 |
| - management                          |      1 | none   | 5      | acc    | ↑ 0.8155 | ± 0.0384 |
| - marketing                           |      1 | none   | 5      | acc    | ↑ 0.8974 | ± 0.0199 |
| - medical_genetics                    |      1 | none   | 5      | acc    | ↑ 0.8200 | ± 0.0386 |
| - miscellaneous                       |      1 | none   | 5      | acc    | ↑ 0.8378 | ± 0.0132 |
| - nutrition                           |      1 | none   | 5      | acc    | ↑ 0.8039 | ± 0.0227 |
| - professional_accounting             |      1 | none   | 5      | acc    | ↑ 0.5532 | ± 0.0297 |
| - professional_medicine               |      1 | none   | 5      | acc    | ↑ 0.7721 | ± 0.0255 |
| - virology                            |      1 | none   | 5      | acc    | ↑ 0.5241 | ± 0.0389 |
| - social sciences                     |      2 | none   | 5      | acc    | ↑ 0.7797 | ± 0.0073 |
| - econometrics                        |      1 | none   | 5      | acc    | ↑ 0.6053 | ± 0.0460 |
| - high_school_geography               |      1 | none   | 5      | acc    | ↑ 0.8485 | ± 0.0255 |
| - high_school_government_and_politics |      1 | none   | 5      | acc    | ↑ 0.9171 | ± 0.0199 |
| - high_school_macroeconomics          |      1 | none   | 5      | acc    | ↑ 0.6923 | ± 0.0234 |
| - high_school_microeconomics          |      1 | none   | 5      | acc    | ↑ 0.7647 | ± 0.0276 |
| - high_school_psychology              |      1 | none   | 5      | acc    | ↑ 0.8697 | ± 0.0144 |
| - human_sexuality                     |      1 | none   | 5      | acc    | ↑ 0.8015 | ± 0.0350 |
| - professional_psychology             |      1 | none   | 5      | acc    | ↑ 0.7271 | ± 0.0180 |
| - public_relations                    |      1 | none   | 5      | acc    | ↑ 0.6818 | ± 0.0446 |
| - security_studies                    |      1 | none   | 5      | acc    | ↑ 0.7224 | ± 0.0287 |
| - sociology                           |      1 | none   | 5      | acc    | ↑ 0.8358 | ± 0.0262 |
| - us_foreign_policy                   |      1 | none   | 5      | acc    | ↑ 0.8900 | ± 0.0314 |
| - stem                                |      2 | none   | 5      | acc    | ↑ 0.5940 | ± 0.0084 |
| - abstract_algebra                    |      1 | none   | 5      | acc    | ↑ 0.3900 | ± 0.0490 |
| - anatomy                             |      1 | none   | 5      | acc    | ↑ 0.6741 | ± 0.0405 |
| - astronomy                           |      1 | none   | 5      | acc    | ↑ 0.7566 | ± 0.0349 |
| - college_biology                     |      1 | none   | 5      | acc    | ↑ 0.8264 | ± 0.0317 |
| - college_chemistry                   |      1 | none   | 5      | acc    | ↑ 0.4700 | ± 0.0502 |
| - college_computer_science            |      1 | none   | 5      | acc    | ↑ 0.5400 | ± 0.0501 |
| - college_mathematics                 |      1 | none   | 5      | acc    | ↑ 0.3900 | ± 0.0490 |
| - college_physics                     |      1 | none   | 5      | acc    | ↑ 0.4314 | ± 0.0493 |
| - computer_security                   |      1 | none   | 5      | acc    | ↑ 0.8000 | ± 0.0402 |
| - conceptual_physics                  |      1 | none   | 5      | acc    | ↑ 0.6170 | ± 0.0318 |
| - electrical_engineering              |      1 | none   | 5      | acc    | ↑ 0.6552 | ± 0.0396 |
| - elementary_mathematics              |      1 | none   | 5      | acc    | ↑ 0.4735 | ± 0.0257 |
| - high_school_biology                 |      1 | none   | 5      | acc    | ↑ 0.8097 | ± 0.0223 |
| - high_school_chemistry               |      1 | none   | 5      | acc    | ↑ 0.6207 | ± 0.0341 |
| - high_school_computer_science        |      1 | none   | 5      | acc    | ↑ 0.7300 | ± 0.0446 |
| - high_school_mathematics             |      1 | none   | 5      | acc    | ↑ 0.4222 | ± 0.0301 |
| - high_school_physics                 |      1 | none   | 5      | acc    | ↑ 0.4636 | ± 0.0407 |
| - high_school_statistics              |      1 | none   | 5      | acc    | ↑ 0.6065 | ± 0.0333 |
| - machine_learning                    |      1 | none   | 5      | acc    | ↑ 0.5446 | ± 0.0473 |
| gsm8k                                 |      2 | flexible-extract | 5      | exact_match | ↑ 0.8446 | ± 0.0100 |