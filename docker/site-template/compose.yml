services:
  db:
    image: mariadb:11
    container_name: __SITE__-db
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    environment:
      MARIADB_DATABASE: ${DB_NAME}
      MARIADB_USER: ${DB_USER}
      MARIADB_PASSWORD: ${DB_PASSWORD}
      MARIADB_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
    volumes:
      - db_data:/var/lib/mysql
    mem_limit: ${DB_MEMORY}
    cpus: 0.50
    networks:
      - internal
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 20

  wordpress:
    image: wordpress:latest
    container_name: __SITE__-wp
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    depends_on:
      db:
        condition: service_healthy
    environment:
      WORDPRESS_DB_HOST: db:3306
      WORDPRESS_DB_NAME: ${DB_NAME}
      WORDPRESS_DB_USER: ${DB_USER}
      WORDPRESS_DB_PASSWORD: ${DB_PASSWORD}
      WORDPRESS_CONFIG_EXTRA: |
        define('WP_HOME', 'https://__DOMAIN__');
        define('WP_SITEURL', 'https://__DOMAIN__');
    volumes:
      - ./wordpress:/var/www/html
    mem_limit: ${WP_MEMORY}
    cpus: ${WP_CPUS}
    networks:
      - internal
      - wp-proxy

volumes:
  db_data:

networks:
  internal:
    internal: true
  wp-proxy:
    external: true
    name: __PROXY_NETWORK__
