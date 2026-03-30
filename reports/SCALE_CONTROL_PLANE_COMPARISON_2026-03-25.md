# MX Scale Control-Plane Comparison

- runs: `9`

## Per Run
- lanes=`100` seed=`7` passed=`True` verified=`75/150` conv_s=`45.0` queue_p95=`90.0` settle_p95=`20.0` dlq=`73` cost_per_success=`0.0439` governor=`7` artifact=`reports/scale_control_plane/20260325T183054Z`
- lanes=`100` seed=`11` passed=`True` verified=`100/150` conv_s=`65.0` queue_p95=`90.0` settle_p95=`22.75` dlq=`44` cost_per_success=`0.0423` governor=`10` artifact=`reports/scale_control_plane/20260325T183055Z`
- lanes=`100` seed=`19` passed=`True` verified=`91/150` conv_s=`55.0` queue_p95=`90.0` settle_p95=`24.75` dlq=`54` cost_per_success=`0.0461` governor=`9` artifact=`reports/scale_control_plane/20260325T183056Z`
- lanes=`500` seed=`7` passed=`True` verified=`447/750` conv_s=`75.0` queue_p95=`490.0` settle_p95=`25.0` dlq=`287` cost_per_success=`0.0429` governor=`7` artifact=`reports/scale_control_plane/20260325T183057Z`
- lanes=`500` seed=`11` passed=`True` verified=`460/750` conv_s=`55.0` queue_p95=`490.0` settle_p95=`20.0` dlq=`268` cost_per_success=`0.0416` governor=`3` artifact=`reports/scale_control_plane/20260325T183058Z`
- lanes=`500` seed=`19` passed=`True` verified=`496/750` conv_s=`75.0` queue_p95=`490.0` settle_p95=`20.0` dlq=`231` cost_per_success=`0.0413` governor=`19` artifact=`reports/scale_control_plane/20260325T183059Z`
- lanes=`1000` seed=`7` passed=`True` verified=`912/1500` conv_s=`65.0` queue_p95=`1000.0` settle_p95=`20.0` dlq=`555` cost_per_success=`0.0413` governor=`5` artifact=`reports/scale_control_plane/20260325T183100Z`
- lanes=`1000` seed=`11` passed=`True` verified=`847/1500` conv_s=`75.0` queue_p95=`1000.0` settle_p95=`20.0` dlq=`597` cost_per_success=`0.0436` governor=`11` artifact=`reports/scale_control_plane/20260325T183102Z`
- lanes=`1000` seed=`19` passed=`True` verified=`864/1500` conv_s=`60.0` queue_p95=`1000.0` settle_p95=`20.0` dlq=`603` cost_per_success=`0.0409` governor=`8` artifact=`reports/scale_control_plane/20260325T183103Z`

## Aggregates By Lane Count
- lanes=`100` pass_rate=`3/3` avg_verified=`88.7` avg_convergence_s=`55.0` avg_queue_p95=`90.0` avg_settle_p95=`22.5` avg_dlq=`57.0` avg_cost_per_success=`0.0441` avg_governor=`8.7`
- lanes=`500` pass_rate=`3/3` avg_verified=`467.7` avg_convergence_s=`68.3` avg_queue_p95=`490.0` avg_settle_p95=`21.7` avg_dlq=`262.0` avg_cost_per_success=`0.0419` avg_governor=`9.7`
- lanes=`1000` pass_rate=`3/3` avg_verified=`874.3` avg_convergence_s=`66.7` avg_queue_p95=`1000.0` avg_settle_p95=`20.0` avg_dlq=`585.0` avg_cost_per_success=`0.0419` avg_governor=`8.0`
