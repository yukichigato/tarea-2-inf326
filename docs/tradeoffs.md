# Discusión de trade-offs para la USM

La USM, como mandante, necesita un acortador que cree URLs cortas y redirija a las URLs originales. Las dos arquitecturas implementadas cumplen esos requisitos funcionales y difieren en los atributos de calidad que favorecen. La arquitectura 1 favorece la analítica de visitas, mientras que la arquitectura 2 favorece el desempeño y una operación más simple. A continuación se comparan ambas en desempeño, disponibilidad, consistencia, operación y seguridad, se indica para cada trade-off si es de consideración para la USM y se evalúa cuál conviene para distintos usos. El equipo no dispone de datos de tráfico de la USM. Por eso, los juicios se apoyan en las [mediciones locales](#mediciones-locales), que permiten comparar las arquitecturas entre sí, pero no representan condiciones de producción ni el tráfico real de la universidad.

## Arquitectura 1: redirección temporal con registro de visitas

El acortador de la arquitectura 1 responde con 302 y `Cache-Control: no-store`, una directiva que prohíbe a las cachés conformes guardar la respuesta ([RFC 9111, §5.2.2.5](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5)). Por eso, cada visita llega al acortador, que envía al servicio Log un evento `url hit` con el hash del enlace corto, la URL larga y el timestamp. El acortador redirige recién cuando Log confirma la escritura, de modo que cada redirección entregada tiene un evento registrado.

Este beneficio tiene cuatro costos. El primero es la latencia: cada visita suma una llamada gRPC síncrona con TLS y una escritura en la base del registro. En las mediciones locales, una visita a la arquitectura 1 tardó 8,0 ms en la mediana, frente a 1,9 ms en la arquitectura 2: unos 6 ms más. **No es de consideración para la USM en operación normal**, porque la diferencia medida es de pocos milisegundos por visita. Esta conclusión vale mientras Log responda; cuando no responde, el problema pasa a ser de disponibilidad.

El segundo costo es la disponibilidad. La redirección depende de dos componentes: cuando Log está detenido o no responde dentro de `GRPC_TIMEOUT`, el acortador responde 503, y en el despliegue con Docker ambos casos tardaron los 2 s del plazo. Con ello, el diseño prioriza que el registro esté completo por sobre que la redirección esté disponible. Mientras espera a Log, cada visita ocupa uno de los hilos del servidor HTTP, así que también se demoran las demás peticiones. En una medición local con Log congelado, 100 visitas simultáneas recibieron 503 a un ritmo de unas 7 por segundo, y una creación de URL enviada en paralelo tardó 11,8 s, cuando normalmente tarda unos 6 ms. **Es de consideración para la USM**: si Log falla, ningún enlace de la arquitectura 1 redirige, y si queda congelado se degradan también la creación de URLs y `/health`.

El tercer costo es la consistencia. El acortador y Log confirman sus escrituras por separado, sin una transacción común. Un timeout puede ocurrir después de que Log guardó el evento; en ese caso, el registro contiene un evento de una visita que recibió 503. La prueba `test_grpc_timeout_can_be_followed_by_a_committed_event` reproduce este caso, que se explica por el funcionamiento de los [deadlines de gRPC](https://grpc.io/docs/guides/deadlines/). Log descarta el evento si, antes de escribirlo, detecta que el acortador dejó de esperar, pero eso no evita el caso en que la escritura termina justo después del plazo. Si el usuario reintenta, se agrega otro evento. El diseño asegura un evento por cada 302, y el registro puede contener además eventos de visitas fallidas. **No es de consideración para medir campañas**: esas diferencias solo aparecen cuando Log falla o se demora, y en ese caso el registro puede contar de más, pero nunca omite una visita que recibió 302. Lo sería si el registro tuviera que servir como respaldo exacto, por ejemplo para una auditoría, que no es el uso planteado.

El cuarto costo es operativo, porque la arquitectura requiere mantener dos servicios y dos bases de datos. **Por sí solo no es de consideración en este despliegue**: Log agrega un contenedor y un volumen en el mismo nodo. Su efecto relevante es el de disponibilidad, ya descrito.

## Arquitectura 2: redirección permanente con rate limiter y caché

El acortador de la arquitectura 2 responde con 301 y `Cache-Control: public, max-age=60`, lo que autoriza al navegador a guardar la redirección durante 60 segundos. Mientras su copia está vigente, el navegador resuelve las visitas repetidas sin contactar al servidor. El valor de 60 segundos es deliberado. La API no permite cambiar ni desactivar un enlace, pero la USM podría necesitar retirar uno directamente de la base, por ejemplo un enlace malicioso, y si la base se recreara, los hashes volverían a asignarse desde `597652313` a otros destinos. Con `max-age=60`, los navegadores dejan de usar una copia desactualizada en poco tiempo; sin esa cabecera, un 301 puede quedar guardado por tiempo indefinido. El costo es que el caché del navegador solo evita las visitas repetidas del mismo navegador dentro de un minuto.

En el servidor, el caché en memoria de LiteStar implementa la táctica de mantener múltiples copias de datos: entrega la respuesta sin consultar SQLite durante `CACHE_TTL` segundos. En las mediciones locales, una redirección servida desde el caché tardó 1,0 ms en la mediana, frente a 1,9 ms cuando consultó la base. **No es de consideración por latencia**, porque ahorra menos de 1 ms. Su posible beneficio bajo carga sería reducir las consultas a la base, pero no se midió.

El rate limiter de LiteStar implementa la táctica de controlar el arribo de eventos, que la clase define mediante un SLA con una tasa máxima de arribo. **El SLA de esta implementación es de 60 peticiones por minuto por IP** (`RATE_LIMIT_REQUESTS`): dentro de ese límite, el servicio atiende las peticiones; por sobre él, responde 429 sin resolver la URL. Es el SLA de la entrega evaluada, no una capacidad validada para producción: las mediciones locales no estiman cuántas peticiones soportaría el servidor con tráfico real. La cuota se cuenta en una ventana fija de un minuto, que empieza con la primera petición de cada IP, así que en 60 segundos que crucen el cambio de ventana pueden pasar hasta el doble de peticiones. Así, un cliente abusivo consume una cantidad acotada de trabajo del servidor. Además, esta arquitectura funciona con un solo servicio y una sola base.

Estos beneficios también tienen costos. El principal es que la arquitectura 2 no registra visitas: no tiene un servicio como Log, y un registro agregado en el servidor tampoco vería las visitas que el navegador resuelve con su copia, aunque con `max-age=60` esas son solo las repeticiones dentro de un minuto. **Es de consideración para los enlaces que la USM necesita medir**, y por eso la tabla de conveniencia los asigna a la arquitectura 1. No lo es para los enlaces que solo necesitan redirigir.

El servidor tampoco controla las copias ya entregadas. La API actual mantiene fijo el destino de cada enlace, pero la página de destino puede cambiar o desaparecer, y mientras una copia está vigente el navegador no contacta al servidor, así que este no puede invalidarla. Con la configuración actual, una copia desactualizada dura como máximo unos dos minutos: hasta 60 s en el caché del servidor y hasta 60 s más en el navegador, porque la respuesta cacheada conserva `max-age=60`. **No es de consideración con este valor**; lo sería con un `max-age` largo.

El rate limiter calcula la cuota por IP. Los usuarios que salen a internet por la misma IP, como los de una red con NAT, comparten una cuota y pueden recibir 429 aunque hagan un uso legítimo. En el despliegue con Docker de esta entrega ocurre lo mismo: el log de acceso de la arquitectura 2 registra las peticiones del host con la IP del gateway de la red de Compose, mientras que una petición desde otro contenedor aparece con la IP de ese contenedor, así que todas las peticiones del host comparten una sola cuota. Detrás de un balanceador o un proxy inverso, todos los usuarios llegarían con la IP del proxy. En ese caso habría que ajustar la política o identificar al cliente con la cabecera `X-Forwarded-For`, pero solo cuando la agrega un proxy de confianza: si se aceptara de cualquier cliente, bastaría con falsificarla para evadir el límite. LiteStar no la usa por defecto, y esta implementación tampoco. **Es de consideración para la USM**: si muchos usuarios abren un enlace desde una red que sale a internet por pocas IP, como podría ser la de un campus, un límite de 60 por minuto por IP rechazaría visitas legítimas. El valor tendría que revisarse con datos de tráfico, que el equipo no tiene.

Por último, el caché y las cuotas residen en la memoria de un proceso. Se pierden al reiniciarlo, y cada réplica tendría los suyos. El servicio purga cada 60 segundos las entradas vencidas, porque el almacén de LiteStar solo las descarta cuando se vuelve a leer su clave. Además, cuando una ráfaga encuentra el caché vacío, varias peticiones pueden consultar la base antes de que exista una copia. **No es de consideración en este despliegue de un solo proceso**: perder el caché solo provoca nuevas lecturas a la base, y perder las cuotas reinicia los contadores. Lo sería con varias réplicas, porque cada una aplicaría su propia cuota.

## Seguridad: creación anónima de enlaces

Ambas arquitecturas permiten que cualquiera cree enlaces sin autenticarse. Bajo un dominio institucional, eso tiene riesgos identificados. Un enlace corto oculta su destino y quien lo recibe solo ve el dominio de la USM, así que podría usarse para phishing o para distribuir contenido malicioso. Si ese abuso ocurre, la reputación del dominio puede verse afectada, por ejemplo si servicios de filtrado de correo o de navegación lo incluyen en listas de bloqueo. Además, los hashes son consecutivos, por lo que es posible enumerar los enlaces existentes.

La implementación actual solo tiene protecciones parciales: acepta únicamente URLs `http` y `https`, rechaza las URLs del propio acortador y, en la arquitectura 2, el rate limiter acota cuántos enlaces crea cada IP por minuto. No tiene autenticación, revisión de destinos ni una forma de desactivar enlaces desde la API, y la arquitectura 1 no limita la creación. Las medidas que mitigarían estos riesgos, y que no están implementadas, son exigir autenticación institucional para crear enlaces, revisar los destinos contra listas de sitios maliciosos, permitir desactivar enlaces y usar hashes no consecutivos. **Es de consideración para la USM** antes de abrir la creación de enlaces fuera de un entorno controlado.

## Comparación por camino de ejecución

La siguiente tabla compara el trabajo que realiza cada arquitectura en cada camino. Los tiempos que aparecen son parámetros configurados, y `GRPC_TIMEOUT` acota solo la llamada gRPC dentro de la petición HTTP.

| Camino | Arquitectura 1 | Arquitectura 2 |
| --- | --- | --- |
| Creación de una URL (`POST`) | El acortador valida la URL y la inserta en SQLite. | El rate limiter descuenta la cuota, y luego el acortador valida e inserta la URL. |
| Visita a un hash existente | El acortador lee la URL en SQLite, espera la confirmación de Log y responde 302. | El rate limiter descuenta la cuota, y el acortador usa la copia del caché o lee SQLite antes de responder 301. |
| Visita repetida desde el mismo navegador | El navegador repite el camino completo. | El navegador usa su copia y evita contactar al servidor mientras esa copia esté vigente, hasta 60 s. |
| Visita a un hash inexistente | El acortador responde 404 sin generar un evento. | El rate limiter descuenta la cuota, y el acortador responde 404 sin guardar la respuesta en el caché. |
| Log lento o detenido | El acortador espera hasta `GRPC_TIMEOUT` y responde 503. | El acortador sigue funcionando, porque no usa Log. |
| Exceso de peticiones | El acortador atiende todas las peticiones. | El rate limiter responde 429 sin resolver la URL. |

## Mediciones locales

Las cifras de esta discusión provienen de mediciones hechas el 16 de noviembre de 2026 en un solo equipo, con WSL2 y Docker Desktop: el cliente y los servicios corrían en la misma máquina, sin latencia de red real. Sirven para comparar las arquitecturas entre sí; **no representan condiciones de producción ni el tráfico real de la USM**, que el equipo no conoce. Cada camino se midió con 300 peticiones secuenciales que reutilizaban la conexión.

| Camino | Mediana | Percentil 95 |
| --- | --- | --- |
| Creación de una URL (`POST`), en cualquiera de las dos arquitecturas | 6,2 ms | 7,2–7,4 ms |
| Visita a la arquitectura 1 (lectura en SQLite y registro en Log) | 8,0 ms | 11,4 ms |
| Visita a la arquitectura 2 que consulta la base | 1,9 ms | 2,5 ms |
| Visita a la arquitectura 2 servida desde el caché | 1,0 ms | 1,3 ms |

Además, con Log congelado (`docker compose pause log`), 100 visitas simultáneas a la arquitectura 1 recibieron 503 a un ritmo de unas 7 por segundo, y la más lenta tardó 14,1 s.

## ¿Son de consideración para la USM?

| Trade-off | Arquitectura | ¿De consideración? | Motivo |
| --- | --- | --- | --- |
| Latencia del registro | 1 | No, en operación normal | Unos 6 ms más por visita en las mediciones locales. |
| Disponibilidad | 1 | Sí | Si Log falla, ningún enlace redirige; si se congela, también se degradan la creación de URLs y `/health`. |
| Consistencia entre registro y redirecciones | 1 | No, para medir campañas | El registro puede contar de más cuando Log falla, pero no omite visitas que recibieron 302. |
| Operación de dos servicios | 1 | No, por sí sola | Su efecto relevante es el de disponibilidad. |
| Falta de registro de visitas | 2 | Sí, para los enlaces que se quieren medir | La arquitectura 2 no tiene Log. |
| Copias que el servidor no controla | 2 | No, con `max-age=60` | Una copia desactualizada dura como máximo unos 2 minutos. |
| Caché del servidor | 2 | No, por latencia | Ahorra menos de 1 ms; su efecto bajo carga no se midió. |
| Cuota por IP | 2 | Sí | Con NAT o un proxy, usuarios legítimos comparten una cuota de 60 por minuto. |
| Estado en memoria | 2 | No, con un solo proceso | Solo provoca nuevas lecturas a la base o reinicia contadores. |
| Creación anónima de enlaces | Ambas | Sí | Riesgo de phishing y de daño a la reputación del dominio, con protecciones solo parciales. |

## Conveniencia para la USM

| Uso en la USM | Arquitectura | Razón |
| --- | --- | --- |
| Campañas que la USM necesita medir, como la postulación a postgrado del enunciado, la admisión o la inscripción a eventos. | 1 | La arquitectura 1 registra cada visita con el hash del enlace, así que distingue los enlaces de una campaña que llevan a la misma página, por ejemplo uno por canal de difusión. En la 2, las visitas resueltas por el navegador quedan sin registrar. |
| Enlaces estables en reglamentos, documentos oficiales o códigos QR impresos, si no necesitan medición. | 2 | El destino se mantiene en el tiempo, y la arquitectura 2 los redirige sin depender de Log, que es el principal riesgo de disponibilidad de la 1. |
| Publicaciones con mucha demanda simultánea, como resultados o matrícula. | 2, con la cuota revisada | La arquitectura 2 no depende de Log, y su caché evita consultas repetidas a la base, aunque ese beneficio bajo carga no se midió. Una cuota de 60 por minuto por IP rechazaría a usuarios que comparten red, así que habría que revisarla. |

La arquitectura 1 obtiene información de uso a cambio de latencia y disponibilidad. La arquitectura 2 obtiene desempeño y simplicidad a cambio de la analítica y del control sobre las copias. La conveniencia de cada una depende del propósito de cada enlace, y la USM podría operar ambas. En ese caso, cada arquitectura tendría que usar un dominio o un prefijo propio, o ambas tendrían que compartir el espacio de `id`: hoy cada una tiene su base y ambas secuencias parten en `597652313`, así que el mismo hash, como `eRgfR`, llevaría a destinos distintos en cada arquitectura. Es el problema de unicidad que trata el bonus.

Una alternativa intermedia, fuera de esta implementación, consiste en responder 302 sin esperar a Log y enviar el evento después mediante una cola. Con ella, la redirección dejaría de depender del registro. Sin embargo, una cola en memoria perdería los eventos pendientes al reiniciarse, y una cola persistente agregaría otro componente al despliegue.

El evento de la arquitectura 1 guarda solo el hash del enlace, la URL y la hora de cada visita. Registrar la IP u otros identificadores del visitante convertiría el log en un registro de datos personales, y la USM tendría que definir su finalidad y su plazo de retención. La arquitectura 2 usa la IP para calcular la cuota y la mantiene solo en memoria.

## Evidencia y datos desconocidos

Las pruebas automatizadas, descritas en el [README](../README.md#pruebas), verifican la concurrencia en la asignación de identificadores, en el registro gRPC y en el rate limiter, y reproducen el timeout ambiguo de gRPC. La IP que observa el rate limiter en Docker se verificó aparte, el 16 de noviembre de 2026, en el log de acceso de la arquitectura 2: las peticiones del host aparecieron con `172.26.0.1`, el gateway de la red de Compose en esa prueba, y una petición desde el contenedor de Log apareció con la IP de ese contenedor. La dirección exacta depende de la red que Docker asigne en cada equipo.

`GRPC_TIMEOUT=2` es un parámetro elegido para la entrega, independiente de cualquier estimación de capacidad. `CACHE_TTL=60` se justifica en la sección de la arquitectura 2, y `RATE_LIMIT_REQUESTS=60` es el SLA declarado de la entrega. El volumen de visitas de la USM, su distribución en el tiempo y la tasa de acierto del caché son desconocidos, porque el equipo no tiene datos de tráfico de la universidad. Por eso, las conclusiones sobre rendimiento se limitan a comparar las arquitecturas con las mediciones locales.

## Bonus: unicidad de identificadores en un ambiente distribuido

La codificación base62 asigna un hash distinto a cada número, así que los hashes son únicos cuando los `id` lo son. La secuencia SQLite actual garantiza `id` únicos dentro de un nodo. En cambio, dos nodos con bases independientes generarían los mismos `id` y, por lo tanto, los mismos hashes. Un ambiente distribuido necesita una estrategia que impida entregar el mismo `id` dos veces. La tabla describe tres estrategias:

| Estrategia | Funcionamiento | Condición | Costo |
| --- | --- | --- | --- |
| Secuencia central | Una base autoritativa asigna cada `id`. En PostgreSQL, [`nextval`](https://www.postgresql.org/docs/current/functions-sequence.html) lo hace de forma atómica. | La base debe ser el único escritor, y un failover debe continuar la secuencia sin reutilizar IDs. | Cada creación depende de la disponibilidad de esa base. |
| Rangos disjuntos | Un asignador entrega a cada nodo un bloque de IDs sin solapamiento, y el nodo asigna localmente dentro de su bloque. | El asignador debe persistir cada bloque antes de entregarlo y nunca reasignarlo. | Los IDs dejan de crecer en orden entre nodos, y los bloques sin usar dejan huecos. |
| ID compuesto, al estilo Snowflake | Cada nodo arma el `id` con el tiempo, su número de nodo y un contador local. | Cada nodo debe tener un número único y un reloj que siempre avance. | Los hashes se alargan, y los IDs dejan de partir en `597652313` con incremento de 1. |

Si todos los nodos escriben en una base común, la restricción `UNIQUE` sobre `hash` hace que esa base rechace un hash repetido si la estrategia llegara a fallar. Con bases independientes por nodo, que es el caso de los rangos disjuntos y de los IDs compuestos, cada restricción `UNIQUE` solo ve los hashes de su propio nodo y no detecta duplicados entre nodos, así que la unicidad depende por completo de la estrategia. Con un solo punto de escritura, la secuencia central es la opción más simple. Si la USM agregara nodos de escritura, los rangos disjuntos conservarían hashes cortos y consultarían al asignador solo al agotar cada bloque. Los UUID aleatorios y el hash de la URL larga reducen la probabilidad de colisión, pero no la eliminan sin una verificación adicional, así que tampoco aseguran la unicidad que exige el enunciado.

## Referencias

- L. Bass, P. Clements y R. Kazman, *Software Architecture in Practice*, 4.ª ed., Addison-Wesley, 2021.
- [RFC 9111: HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html).
- [gRPC: Deadlines](https://grpc.io/docs/guides/deadlines/).
- [PostgreSQL: Sequence Manipulation Functions](https://www.postgresql.org/docs/current/functions-sequence.html).
