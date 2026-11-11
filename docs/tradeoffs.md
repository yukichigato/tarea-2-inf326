# Discusión de trade-offs para la USM

Las dos arquitecturas resuelven los mismos requisitos funcionales, pero privilegian atributos de calidad distintos. La arquitectura 1 privilegia la **analítica**; la arquitectura 2, el **desempeño** y la **simplicidad operativa**. Esta discusión compara qué gana y qué cede cada una en desempeño, disponibilidad, consistencia y operación, y evalúa su conveniencia para la USM como mandante. No se dispone de tráfico real de la USM, por lo que el análisis es cualitativo: no se estiman volúmenes de visitas, tasas de acierto del caché ni latencias.

## 1. Arquitectura 1: redirección temporal con registro de visitas

**Beneficio.** La respuesta 302 va acompañada de `Cache-Control: no-store`, que prohíbe guardarla en cachés conformes ([RFC 9111, §5.2.2.5](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5)). Por eso, cada visita pasa por el acortador y genera un evento `url hit` con la URL y el timestamp. El acortador redirige solo después de que el servicio Log confirma la escritura. Así, toda redirección entregada tiene su evento registrado.

**Costos.**

- **Latencia.** Cada visita suma una llamada gRPC síncrona con TLS y una escritura en la base del registro.
- **Disponibilidad.** Para redirigir se necesitan dos componentes en funcionamiento. Si Log no responde dentro de `GRPC_TIMEOUT`, el acortador devuelve 503: se prefiere no redirigir antes que perder un evento. Del código se deduce, sin haberlo medido, que mientras Log esté lento cada visita ocupa un hilo del servidor HTTP. Si se ocupan todos, también se retrasan las creaciones de URLs.
- **Consistencia.** No hay una transacción común entre el acortador y Log. Un timeout puede ocurrir después de que Log ya guardó el evento, y en ese caso existe un evento sin redirección; `test_grpc_timeout_can_be_followed_by_a_committed_event` lo reproduce ([deadlines en gRPC](https://grpc.io/docs/guides/deadlines/)). Si el usuario reintenta, se genera otro evento. Se garantiza que cada 302 tiene un evento, pero no que cada evento corresponda a una redirección.
- **Operación.** Se mantienen dos servicios y dos bases de datos.

## 2. Arquitectura 2: redirección permanente con rate limiter y caché

**Beneficios.**

- **Caché del navegador.** La respuesta 301 con `Cache-Control: public, max-age` permite que el navegador guarde la redirección. Mientras esa copia esté vigente, las visitas repetidas no llegan al servidor.
- **Mantener múltiples copias de datos.** El caché en memoria de LiteStar responde sin consultar SQLite mientras la copia está vigente (`CACHE_TTL`, 60 s por defecto).
- **Controlar el arribo de eventos.** El rate limiter acota las peticiones de cada IP (`RATE_LIMIT_REQUESTS`, 60 por minuto por defecto) y rechaza el exceso con 429 antes de resolver la URL. Esto protege el servicio frente a clientes abusivos.
- **Simplicidad.** Solo hay un servicio y una base; no depende de Log.

**Costos.**

- **Sin analítica.** Las visitas resueltas por el navegador nunca llegan al servidor. Aunque se agregara un registro, no se podrían contar.
- **Copias que el servidor no controla.** La API no permite editar enlaces. Las páginas de destino, sin embargo, pueden cambiar o desaparecer. Si en el futuro se corrigiera un enlace, los navegadores seguirían usando su copia hasta que venciera, y el servidor no tiene forma de invalidarla.
- **Cuota por IP.** Los usuarios que comparten una misma IP de salida, como la red de un campus, comparten también la cuota y pueden recibir 429 sin ser abusivos. En el despliegue con Docker de esta entrega, todas las peticiones del host llegan con la IP del gateway de Docker, como se observó en su log de acceso.
- **Estado local.** El caché y las cuotas viven en la memoria de un proceso: se pierden al reiniciarlo y no se comparten entre réplicas. Además, cuando una ráfaga llega con el caché vacío, varias peticiones pueden consultar la base antes de que exista una copia.

## 3. Comparación por camino de ejecución

| Camino | Arquitectura 1 | Arquitectura 2 |
| --- | --- | --- |
| Crear URL (`POST`) | Validación e inserción en SQLite. | Igual, tras el control de cuota. |
| Hash válido | Lectura en SQLite y registro confirmado; luego 302. | Cuota; copia en caché o lectura en SQLite; luego 301. |
| Visita repetida del mismo navegador | Repite el camino completo. | No llega al servidor mientras la copia del navegador esté vigente. |
| Hash inexistente | 404 sin evento. | Cuota y 404; no se guarda en caché. |
| Log lento o caído | 503 tras esperar hasta `GRPC_TIMEOUT`. | No aplica. |
| Exceso de peticiones | Sin límite. | 429 sin resolver la URL. |

Los tiempos de la tabla son parámetros configurados, no mediciones. `GRPC_TIMEOUT` limita solo la llamada gRPC, no la petición HTTP completa.

## 4. Relevancia para la USM

| Uso en la USM | Arquitectura adecuada | Razón |
| --- | --- | --- |
| Campañas que deben medirse, como la postulación a postgrado del enunciado, admisión o eventos. | 1 | Es la única que permite contar visitas. |
| Enlaces estables en reglamentos, documentos oficiales o códigos QR impresos. | 2 | El destino no debería cambiar, y la redirección permanente con caché evita trabajo repetido. |
| Publicaciones con mucha demanda simultánea, como resultados o matrícula. | 2, revisando la cuota | El caché reduce la carga, pero una cuota por IP muy baja puede rechazar a estudiantes que comparten red. |

Ninguna arquitectura domina a la otra. La primera obtiene información a cambio de latencia y disponibilidad; la segunda obtiene desempeño y simplicidad a cambio de analítica y de control sobre las copias. Como la elección depende del propósito de cada enlace, la USM podría operar ambas.

Hay una alternativa intermedia, no implementada: responder 302 sin esperar a Log y enviar el evento después, mediante una cola. La redirección dejaría de depender del registro. A cambio, los eventos podrían perderse (si la cola está en memoria) o se agregaría otro componente (si la cola es persistente).

En cuanto a privacidad, el evento solo guarda la URL y la hora, sin IP ni identificadores del visitante. Registrar más datos convertiría el log en datos personales y exigiría definir finalidad y plazo de retención. La arquitectura 2 usa la IP para la cuota, pero solo en memoria.

## 5. Alcance de la evidencia

Las pruebas automatizadas (ver el [README](../README.md#pruebas)) verifican el comportamiento funcional:

- concurrencia en la asignación de IDs, en el registro gRPC y en el rate limiter;
- el timeout ambiguo de gRPC.

La IP que observa el rate limiter en Docker se midió aparte, en el log de acceso de v2.

Los valores `CACHE_TTL=60`, `RATE_LIMIT_REQUESTS=60` y `GRPC_TIMEOUT=2` son parámetros configurables, no estimaciones de capacidad. Se desconocen el volumen y la distribución de las visitas de la USM, así como la tasa de acierto del caché. Por eso no se afirman mejoras de rendimiento cuantitativas.

## Bonus: identificadores únicos en un ambiente distribuido

Base62 es una biyección: si cada `id` es único, cada hash también lo es. Por eso, el problema se reduce a no entregar nunca el mismo `id` dos veces. La secuencia SQLite actual cumple esto en un solo nodo; dos nodos con bases independientes generarían los mismos hashes. Existen tres estrategias:

| Estrategia | Funcionamiento | Condición | Costo |
| --- | --- | --- | --- |
| Secuencia central | Una base autoritativa asigna cada `id`; por ejemplo, con [`nextval` de PostgreSQL](https://www.postgresql.org/docs/current/functions-sequence.html), que es atómico. | Un solo escritor y un failover que no reutilice IDs. | Cada creación depende de esa base. |
| Rangos disjuntos | Un asignador entrega bloques de IDs sin solapamiento a cada nodo, y cada nodo asigna localmente dentro de su bloque. | El asignador persiste cada bloque antes de entregarlo y nunca lo reasigna. | Los IDs no son crecientes entre nodos y quedan huecos. |
| ID compuesto (estilo Snowflake) | Cada `id` combina tiempo, número de nodo y un contador local. | Números de nodo únicos y relojes que no retrocedan. | Los hashes son más largos y se pierde el inicio en `597652313` con incremento 1. |

En todos los casos, la restricción `UNIQUE` sobre `hash` actúa como última defensa. Con un solo punto de escritura, la secuencia central es lo más simple. Si la USM agregara nodos de escritura, los rangos disjuntos mantendrían hashes cortos y consultarían al asignador solo de vez en cuando. No se recomiendan UUID aleatorios ni el hash de la URL: hacen poco probable una colisión, pero no la impiden.

## Referencias

- L. Bass, P. Clements y R. Kazman, *Software Architecture in Practice*, 4.ª ed., Addison-Wesley, 2021: atributos de calidad y tácticas de desempeño y disponibilidad.
- [RFC 9111: HTTP Caching](https://www.rfc-editor.org/rfc/rfc9111.html).
- [gRPC: Deadlines](https://grpc.io/docs/guides/deadlines/).
- [PostgreSQL: Sequence Manipulation Functions](https://www.postgresql.org/docs/current/functions-sequence.html).
